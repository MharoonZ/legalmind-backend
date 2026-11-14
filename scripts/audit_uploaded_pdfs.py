"""Reconcile PDFs and optionally restore missing bytes without database writes."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import Database
from app.services.legal_extraction import verify_source_pdf


def inspect_local_files(root):
    import fitz
    files = []
    if not root.exists():
        return files
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        item = {"path": str(path.resolve()), "bytes": path.stat().st_size}
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024*1024), b""):
                    digest.update(block)
            item["sha256"] = digest.hexdigest()
            with fitz.open(path) as document:
                item["pages"] = document.page_count
                item["encrypted"] = document.needs_pass
                if not document.needs_pass:
                    item["native_text_characters_by_page"] = [len(page.get_text().strip()) for page in document]
        except Exception as exc:
            item["inspection_error"] = type(exc).__name__
        files.append(item)
    return files


def restore_missing_blob(root, row, matches):
    digest = row["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or row["object_key"] != f"{digest[:2]}/{digest}.pdf":
        raise ValueError("Registered PDF storage identity is invalid")
    destination = (root / row["object_key"]).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("Registered PDF path escapes storage")
    if destination.exists():
        verify_source_pdf(destination, digest)
        return None
    candidates = [m for m in matches if not m.get("inspection_error") and not m.get("encrypted")
                  and m.get("pages") == row["page_count"] and m["bytes"] == row["byte_size"]]
    if not candidates:
        return None
    source = Path(candidates[0]["path"])
    verify_source_pdf(source, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".restore-", delete=False) as stream:
            temporary = Path(stream.name)
            with source.open("rb") as original:
                shutil.copyfileobj(original, stream)
            stream.flush()
            os.fsync(stream.fileno())
        verify_source_pdf(temporary, digest)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            verify_source_pdf(destination, digest)
            return None
        return str(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def audit(output, *, source_dir=None, restore_missing=False):
    settings = get_settings()
    root = Path(settings.legal_upload_dir).resolve()
    # Also discover nested PDFs under the whole existing uploads directory,
    # including contracts, without treating them as statutory sources.
    upload_root = Path("uploads").resolve()
    locals_ = await asyncio.to_thread(inspect_local_files, upload_root)
    if root != upload_root and not root.is_relative_to(upload_root):
        locals_.extend(await asyncio.to_thread(inspect_local_files, root))
    if source_dir is not None:
        source_dir = source_dir.resolve()
        if not source_dir.is_dir():
            raise ValueError("The additional PDF source directory does not exist")
        if source_dir != upload_root and not source_dir.is_relative_to(upload_root) and source_dir != root:
            locals_.extend(await asyncio.to_thread(inspect_local_files, source_dir))
    by_hash = {}
    for item in locals_:
        if item.get("sha256"):
            by_hash.setdefault(item["sha256"], []).append(item)
    db = Database(settings)
    try:
        async with db.session() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            rows = (await session.execute(text("""
                SELECT f.id, f.original_filename, f.object_key, f.sha256, f.byte_size, f.page_count,
                       array_remove(array_agg(DISTINCT i.canonical_title),NULL) AS published_laws
                FROM legal_files f
                LEFT JOIN revision_sources rs ON rs.file_id=f.id
                LEFT JOIN publication_items pi ON pi.revision_id=rs.revision_id
                LEFT JOIN corpus_publications pub ON pub.id=pi.publication_id AND pub.status='active'
                LEFT JOIN legal_instruments i ON i.id=pi.instrument_id AND pub.id IS NOT NULL
                GROUP BY f.id ORDER BY f.original_filename
            """))).mappings().all()
        registered = []
        restored = []
        for row in rows:
            matches = by_hash.get(row["sha256"], [])
            if restore_missing:
                restored_path = await asyncio.to_thread(restore_missing_blob, root, row, matches)
                if restored_path:
                    restored.append(restored_path)
            item = {**dict(row), "id": str(row["id"]), "matching_local_paths": [m["path"] for m in matches],
                    "status": "matched" if matches else "missing_original",
                    "application_object_present": (root / row["object_key"]).is_file()}
            if matches and any(m.get("pages") != row["page_count"] or m["bytes"] != row["byte_size"] for m in matches):
                item["status"] = "metadata_mismatch"
            registered.append(item)
        result = {"uploads_root": str(upload_root), "legal_upload_root": str(root),
                  "additional_source_root": str(source_dir) if source_dir else None,
                  "restored_paths": restored,
                  "local_pdfs": locals_, "registered_files": registered,
                  "summary": {"discovered_pdfs": len(locals_), "registered_files": len(registered),
                              "unique_pdf_hashes": len(by_hash), "restored_originals": len(restored),
                              "missing_application_objects": sum(not r["application_object_present"] for r in registered),
                              "missing_originals": sum(r["status"] == "missing_original" for r in registered)},
                  "scope": "Database is read-only. Explicit restoration copies only hash-matched missing originals; existing files, IDs and source copies remain unchanged. This does not reprocess, approve, or certify legal applicability."}
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
        print(json.dumps(result["summary"]))
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, help="Also inspect an existing PDF folder recursively")
    parser.add_argument("--restore-missing", action="store_true", help="Restore verified missing source bytes to existing storage keys; never write database records")
    try:
        args = parser.parse_args()
        asyncio.run(audit(args.output, source_dir=args.source_dir, restore_missing=args.restore_missing))
    except Exception as exc:
        raise SystemExit(f"PDF reconciliation failed ({type(exc).__name__}).") from None
