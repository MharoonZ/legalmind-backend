"""Preview or create fresh draft structures from selected historical revisions.

Historical drafts are read-only. This utility never approves or publishes a draft.
Run without --write first; --write creates new revisions and processes only those jobs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from app.core.config import get_settings
from app.db.legal_corpus_models import ProcessingJob
from app.db.session import Database
from app.repositories.legal_structure_repository import LegalStructureRepository
from app.services.legal_structure import parse_structure
from app.services.legal_audit import structure_findings


def summarize(nodes) -> dict:
    return {
        "kinds": dict(Counter(node.kind for node in nodes)),
        "sections": [node.label for node in nodes if node.kind == "section"],
        "source_statuses": [
            (node.kind, node.label, node.source_status) for node in nodes
            if getattr(node, "source_status", None)
        ],
    }


async def run(old_ids: list[UUID], write: bool, focus: list[str], raw_contains: list[str],
              compare_ids: list[UUID]) -> None:
    db = Database(get_settings())
    try:
        if not await db.connect():
            raise RuntimeError("Database unavailable")
        repo = LegalStructureRepository(db)
        if write:
            async with db.session() as session:
                pending = (await session.execute(select(ProcessingJob.id).where(
                    ProcessingJob.task_type == "structure",
                    ProcessingJob.status.in_(["queued", "running"]),
                ))).scalars().all()
                if pending:
                    raise RuntimeError(f"Other structure jobs are pending: {pending}; refusing to claim them")
        for old_index, old_id in enumerate(old_ids):
            old = await repo.overview(old_id)
            if old is None:
                raise RuntimeError(f"Historical revision not found: {old_id}")
            documents = await repo.load_sources(old_id)
            preview = parse_structure(documents)
            blocks = {block.id: block for document in documents for page in document.pages
                      for block in page.blocks}
            preview_spans = [(
                SimpleNamespace(**span.__dict__) if hasattr(span, "__dict__") else
                SimpleNamespace(**{field: getattr(span, field) for field in span.__dataclass_fields__}),
                SimpleNamespace(normalized_text=blocks[span.block_id].text,
                                block_kind=blocks[span.block_id].kind,
                                bbox=blocks[span.block_id].bbox),
            ) for span in preview.spans]
            for span, _ in preview_spans:
                span.mapping_quality = "exact"
            source_blocks = [SimpleNamespace(id=block.id, page_id=block.page_id,
                extraction_id=block.extraction_id, page_index=block.page_index,
                block_kind=block.kind, normalized_text=block.text, bbox=block.bbox)
                for block in blocks.values()]
            findings = structure_findings(preview.nodes, preview_spans,
                source_blocks=source_blocks,
                instrument_type=documents[0].instrument_type)
            result = {
                "old_revision": str(old_id), "title": old["title"],
                "old": summarize([type("N", (), item)() for item in old["provisions"]]),
                "preview": summarize(preview.nodes),
                "preview_warning_codes": dict(Counter(w["code"] for w in preview.warnings)),
                "preview_coverage": preview.coverage,
                "preview_audit_findings": [{"code": item["code"], "severity": item["severity"],
                                            "path": item["reference_path"]} for item in findings],
            }
            if compare_ids:
                new_id = compare_ids[old_index]
                saved = await repo.overview(new_id)
                if saved is None or saved["legal_version_id"] != old["legal_version_id"]:
                    raise RuntimeError(f"Comparison revision is missing or belongs to another edition: {new_id}")
                old_audit, new_audit = await repo.audit(old_id), await repo.audit(new_id)
                old_paths = {item["reference_path"] for item in old["provisions"]}
                new_paths = {item["reference_path"] for item in saved["provisions"]}
                result["persisted_comparison"] = {
                    "new_revision": str(new_id), "new": summarize([
                        type("N", (), item)() for item in saved["provisions"]]),
                    "added_paths": sorted(new_paths - old_paths),
                    "removed_paths": sorted(old_paths - new_paths),
                    "old_audit_codes": [item["code"] for item in old_audit["findings"]],
                    "new_audit_codes": [item["code"] for item in new_audit["findings"]],
                }
            if focus:
                by_id = {node.id: node for node in preview.nodes}
                result["focus"] = [{"path": node.reference_path,
                    "parent": by_id[node.parent_id].reference_path if node.parent_id else None,
                    "heading": node.heading, "text": node.own_text[:500],
                    "status": node.source_status}
                    for node in preview.nodes
                    if any(token in node.reference_path for token in focus)]
            if raw_contains:
                result["raw_blocks"] = [{"page": block.page_index, "ordinal": block.ordinal,
                    "text": block.text}
                    for block in blocks.values()
                    if any(token.casefold() in block.text.casefold() for token in raw_contains)]
            if write:
                created = await repo.create_revision(old["legal_version_id"],
                    [item["extraction_id"] for item in old["sources"]])
                new_id = created["revision_id"]
                claim = await repo.claim_next(worker_id="structure-regression-reprocess")
                if claim is None or claim.revision_id != new_id:
                    raise RuntimeError(f"Unexpected claimed job: {claim}")
                try:
                    await repo.complete(claim, preview, documents)
                except Exception as exc:
                    await repo.fail(claim, str(exc))
                    raise
                new = await repo.overview(new_id)
                result["new_revision"] = str(new_id)
                result["new_job_status"] = new["job"]["status"]
                result["audit"] = await repo.audit(new_id)
            print(json.dumps(result, indent=2, default=str))
    finally:
        await db.dispose()


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--old-revision", action="append", type=UUID, required=True)
    cli.add_argument("--write", action="store_true")
    cli.add_argument("--focus", action="append", default=[])
    cli.add_argument("--raw-contains", action="append", default=[])
    cli.add_argument("--compare-revision", action="append", type=UUID, default=[])
    args = cli.parse_args()
    if args.compare_revision and len(args.compare_revision) != len(args.old_revision):
        cli.error("Pass one --compare-revision for each --old-revision, in the same order")
    asyncio.run(run(args.old_revision, args.write, args.focus, args.raw_contains,
                    args.compare_revision))
