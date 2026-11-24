"""Explicit manifest based publication administration; never chooses 'latest' on activation."""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.publication_repository import PublicationRepository
from app.services.corpus_validation import PublicationValidationError


async def run(args):
    db = Database(get_settings())
    try:
        repo = PublicationRepository(db)
        if args.command == "plan":
            result = await repo.plan()
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        elif args.command in {"validate", "create"}:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
            ids = [UUID(value) for value in manifest["chunk_set_ids"]]
            if args.command == "validate":
                result = await repo.validate(ids)
            else:
                result = await repo.create(args.name, ids, notes=args.notes)
        elif args.command == "activate":
            result = await repo.activate(args.publication_id,
                expected_manifest_sha256=args.expected_manifest_sha256)
        else:
            result = await repo.list()
        print(json.dumps(result, indent=2, default=str))
    except PublicationValidationError as exc:
        print(json.dumps({"error": "publication_validation_failed", "issues": exc.issues}, indent=2))
        raise SystemExit(2) from exc
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--output", type=Path)
    for command in ("validate", "create"):
        child = sub.add_parser(command)
        child.add_argument("--manifest", type=Path, required=True)
        if command == "create":
            child.add_argument("--name", required=True)
            child.add_argument("--notes")
    activate = sub.add_parser("activate")
    activate.add_argument("--publication-id", type=UUID, required=True)
    activate.add_argument("--expected-manifest-sha256")
    sub.add_parser("list")
    asyncio.run(run(parser.parse_args()))
