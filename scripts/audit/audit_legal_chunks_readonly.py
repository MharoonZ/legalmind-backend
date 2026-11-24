"""Read-only persisted Phase B audit. Never modifies the database.

Usage: python -m scripts.audit.audit_legal_chunks_readonly [--revision-id UUID]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.db.session import Database
from app.db.streaming import fetch_rows


async def audit(revision_id: UUID | None = None) -> dict:
    db = Database(get_settings())
    try:
        if not await db.connect():
            raise RuntimeError("Database unavailable")
        async with db.session() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            revisions = (await session.execute(text("""
                SELECT r.id, r.legal_version_id, r.revision_number, r.review_state,
                       r.content_sha256, i.canonical_title, v.edition_key
                FROM corpus_revisions r
                JOIN legal_versions v ON v.id = r.legal_version_id
                JOIN legal_instruments i ON i.id = v.instrument_id
                WHERE r.review_state = 'approved' AND
                    (CAST(:rid AS uuid) IS NULL OR r.id = CAST(:rid AS uuid))
                ORDER BY i.canonical_title, r.revision_number
            """), {"rid": revision_id})).mappings().all()
            sets = (await session.execute(text("""
                SELECT s.id, s.revision_id, s.content_sha256, s.chunker_version,
                       s.config_sha256, s.chunk_report,
                       count(DISTINCT c.id) AS chunks,
                       count(l.provision_id) AS links
                FROM chunk_sets s LEFT JOIN legal_chunks c ON c.chunk_set_id = s.id
                LEFT JOIN legal_chunk_provisions l ON l.chunk_id = c.id
                WHERE CAST(:rid AS uuid) IS NULL OR s.revision_id = CAST(:rid AS uuid)
                GROUP BY s.id
            """), {"rid": revision_id})).mappings().all()
            chunks = await fetch_rows(session, text("""
                SELECT c.id, c.chunk_set_id, c.revision_id, c.root_provision_id,
                       c.ordinal, c.chunk_key, c.prefix, c.chunk_text, c.chunk_text_sha256,
                       c.search_text, c.char_count, c.label,
                       r.review_state, r.content_sha256 AS revision_hash,
                       s.content_sha256 AS set_hash
                FROM legal_chunks c JOIN chunk_sets s ON s.id = c.chunk_set_id
                JOIN corpus_revisions r ON r.id = s.revision_id
                WHERE CAST(:rid AS uuid) IS NULL OR c.revision_id = CAST(:rid AS uuid)
            """), {"rid": revision_id}, mappings=True, batch_size=500)
            links = await fetch_rows(session, text("""
                SELECT l.chunk_id, c.chunk_set_id, l.revision_id, l.provision_id, l.ordinal,
                       l.source_span_id, l.provision_char_start, l.provision_char_end,
                       l.chunk_char_start, l.chunk_char_end,
                       p.parent_id, p.kind, p.source_status, p.own_text,
                       p.reference_path, p.ordinal AS provision_ordinal
                FROM legal_chunk_provisions l
                JOIN legal_chunks c ON c.id = l.chunk_id
                JOIN provisions p ON p.id = l.provision_id
                WHERE CAST(:rid AS uuid) IS NULL OR l.revision_id = CAST(:rid AS uuid)
            """), {"rid": revision_id}, mappings=True, batch_size=500)
            provisions = await fetch_rows(session, text("""
                SELECT p.id, p.revision_id, p.parent_id, p.kind, p.source_status,
                       p.ordinal, p.own_text
                FROM provisions p JOIN corpus_revisions r ON r.id = p.revision_id
                WHERE r.review_state = 'approved' AND
                    (CAST(:rid AS uuid) IS NULL OR p.revision_id = CAST(:rid AS uuid))
            """), {"rid": revision_id}, mappings=True, batch_size=500)
            source_positions = await fetch_rows(session, text("""
                SELECT ps.id, ps.provision_id, ps.extraction_id, fp.page_index,
                       b.block_ordinal, ps.block_char_start,
                       ps.provision_char_start, ps.provision_char_end
                FROM provision_spans ps JOIN corpus_revisions r ON r.id = ps.revision_id
                JOIN file_pages fp ON fp.id = ps.page_id
                LEFT JOIN file_blocks b ON b.id = ps.block_id
                WHERE r.review_state = 'approved' AND
                    (CAST(:rid AS uuid) IS NULL OR ps.revision_id = CAST(:rid AS uuid))
            """), {"rid": revision_id}, mappings=True, batch_size=500)
            span_issues = (await session.execute(text("""
                SELECT count(*) FILTER (WHERE substring(p.own_text from
                    ps.provision_char_start + 1 for
                    ps.provision_char_end - ps.provision_char_start) IS DISTINCT FROM
                    substring(b.normalized_text from ps.block_char_start + 1 for
                    ps.block_char_end - ps.block_char_start)) AS bad_exact
                FROM provision_spans ps
                JOIN provisions p ON p.id = ps.provision_id
                LEFT JOIN file_blocks b ON b.id = ps.block_id
                JOIN corpus_revisions r ON r.id = ps.revision_id
                WHERE r.review_state = 'approved' AND ps.mapping_quality = 'exact'
                  AND (CAST(:rid AS uuid) IS NULL OR ps.revision_id = CAST(:rid AS uuid))
            """), {"rid": revision_id})).mappings().one()
            search_stats = (await session.execute(text("""
                SELECT count(DISTINCT c.id) FILTER (WHERE c.search_vector IS DISTINCT FROM
                    to_tsvector('english'::regconfig, c.search_text)) AS bad_vectors,
                       count(DISTINCT c.id) FILTER (WHERE length(btrim(c.chunk_text)) = 0) AS empty_chunks,
                       count(DISTINCT c.id) FILTER (WHERE c.char_count > 4000) AS over_4000_chars,
                       count(*) FILTER (WHERE p.kind = 'other' OR p.source_status IN
                           ('omitted','repealed','deleted')) AS excluded_links,
                       count(*) FILTER (WHERE l.provision_id IS NOT NULL
                           AND p.text_sha256 IS NULL) AS missing_provision_hash_links
                FROM legal_chunks c JOIN chunk_sets s ON s.id = c.chunk_set_id
                LEFT JOIN legal_chunk_provisions l ON l.chunk_id = c.id
                LEFT JOIN provisions p ON p.id = l.provision_id
                WHERE s.chunker_version = 'legal-chunk-v2-source-order'
                  AND (CAST(:rid AS uuid) IS NULL OR c.revision_id = CAST(:rid AS uuid))
            """), {"rid": revision_id})).mappings().one()
            indexes = (await session.execute(text("""
                SELECT indexname, indexdef FROM pg_indexes
                WHERE schemaname = current_schema() AND tablename = 'legal_chunks'
                  AND indexname IN ('ix_legal_chunks_search_vector',
                                    'ix_legal_chunks_chunk_set_label')
            """))).mappings().all()
            duplicate_text_groups = (await session.execute(text("""
                SELECT count(*) FROM (
                    SELECT c.chunk_set_id, c.chunk_text_sha256
                    FROM legal_chunks c JOIN chunk_sets s ON s.id = c.chunk_set_id
                    WHERE s.chunker_version = 'legal-chunk-v2-source-order'
                      AND (CAST(:rid AS uuid) IS NULL OR c.revision_id = CAST(:rid AS uuid))
                    GROUP BY c.chunk_set_id, c.chunk_text_sha256 HAVING count(*) > 1
                ) duplicates
            """), {"rid": revision_id})).scalar_one()
            alias_stats = (await session.execute(text("""
                SELECT count(*) AS instruments,
                       count(*) FILTER (WHERE jsonb_array_length(aliases) > 0) AS with_aliases
                FROM legal_instruments
            """))).mappings().one()
            by_chunk = defaultdict(list)
            for link in links:
                by_chunk[link.chunk_id].append(link)
            set_by_id = {row.id: row for row in sets}
            span_by_id = {row.id: row for row in source_positions}
            repeated_v2_membership_chunks = 0
            issues = Counter()
            samples = defaultdict(list)
            def flag(code: str, value: str) -> None:
                issues[code] += 1
                if len(samples[code]) < 5:
                    samples[code].append(value)
            for chunk in chunks:
                members = sorted(by_chunk[chunk.id], key=lambda item: item.ordinal)
                if not members:
                    flag("chunk_without_provisions", str(chunk.id))
                if chunk.review_state != "approved" or chunk.revision_hash != chunk.set_hash:
                    flag("stale_or_unapproved_chunk", str(chunk.id))
                if len(chunk.chunk_text) != chunk.char_count:
                    flag("wrong_char_count", str(chunk.id))
                if hashlib.sha256(chunk.chunk_text.encode()).hexdigest() != chunk.chunk_text_sha256:
                    flag("wrong_chunk_hash", str(chunk.id))
                if not chunk.search_text.strip():
                    flag("blank_search_text", str(chunk.id))
                is_v2 = set_by_id[chunk.chunk_set_id].chunk_report.get("format") == "source_fragments_v2"
                if is_v2 and len(members) != len({m.provision_id for m in members}):
                    repeated_v2_membership_chunks += 1
                if not is_v2 and len(members) != len({m.provision_id for m in members}):
                    flag("duplicate_membership_within_chunk", str(chunk.id))
                if any(m.revision_id != chunk.revision_id for m in members):
                    flag("cross_revision_membership", str(chunk.id))
                if is_v2:
                    # Reconstruct the whole passage, including deliberate
                    # separators, not merely individual source slices.
                    rebuilt = chunk.prefix + "\n\n"
                    previous = None
                    for member in members:
                        if previous is not None and not (
                            previous.provision_id == member.provision_id and
                            previous.provision_char_end == member.provision_char_start
                        ):
                            rebuilt += "\n\n"
                        rebuilt += member.own_text[
                            member.provision_char_start:member.provision_char_end]
                        previous = member
                    if rebuilt != chunk.chunk_text:
                        flag("v2_full_reconstruction_mismatch", str(chunk.id))
                    for member in members:
                        if member.source_span_id is None:
                            flag("v2_missing_source_span", str(chunk.id))
                        elif (chunk.chunk_text[member.chunk_char_start:member.chunk_char_end]
                              != member.own_text[member.provision_char_start:member.provision_char_end]):
                            flag("v2_fragment_text_mismatch", str(chunk.id))
                        else:
                            span = span_by_id.get(member.source_span_id)
                            if (span is None or span.provision_id != member.provision_id or
                                not member.provision_char_start <= span.provision_char_start or
                                not member.provision_char_end >= span.provision_char_end):
                                flag("v2_wrong_source_span_link", str(chunk.id))
                # A continuation chunk normally contains only descendants;
                # the root is still pinned by root_provision_id.
            membership = Counter((link.chunk_set_id, link.provision_id) for link in links)
            for (set_id, pid), count in membership.items():
                if count > 1 and set_by_id[set_id].chunk_report.get("format") != "source_fragments_v2":
                    flag("provision_in_multiple_chunks", f"{set_id}:{pid}:{count}")
            for row in sets:
                if row.chunks != row.chunk_report.get("chunk_count"):
                    flag("set_report_count_mismatch", str(row.id))
                if row.chunks == 0:
                    flag("empty_chunk_set", str(row.id))
            provision_by_id = {p.id: p for p in provisions}
            positions = defaultdict(list)
            for item in source_positions:
                if item.block_ordinal is not None and item.block_char_start is not None:
                    positions[item.provision_id].append((item.extraction_id,
                        item.page_index, item.block_ordinal, item.block_char_start))
            interleaved_parents = set()
            for p in provisions:
                if p.parent_id not in provision_by_id:
                    continue
                parent_locs = positions[p.parent_id]
                child_locs = positions[p.id]
                if any(a[0] == b[0] and a[1:] > b[1:]
                       for a in parent_locs for b in child_locs):
                    interleaved_parents.add(p.parent_id)
            v2_by_root = defaultdict(list)
            for chunk in chunks:
                if set_by_id[chunk.chunk_set_id].chunk_report.get("format") != "source_fragments_v2":
                    continue
                for link in by_chunk[chunk.id]:
                    span = span_by_id.get(link.source_span_id)
                    if span is not None:
                        v2_by_root[(chunk.chunk_set_id, chunk.root_provision_id)].append(
                            (chunk.ordinal, link.ordinal, span.extraction_id,
                             span.page_index, span.block_ordinal, span.block_char_start))
            for (set_id, root_id), entries in v2_by_root.items():
                ordered = sorted(entries)
                for previous, current in zip(ordered, ordered[1:]):
                    if previous[2] == current[2] and previous[3:] > current[3:]:
                        flag("v2_source_order_violation", f"{set_id}:{root_id}")
                        break
            # A v1 chunk always places all parent text before its children.
            # If an exact parent span is physically after a child, that
            # ordering is confirmed wrong, even when string offsets match.
            v1_membership = defaultdict(list)
            chunk_ordinals = {c.id: c.ordinal for c in chunks}
            for link in links:
                if set_by_id[link.chunk_set_id].chunk_report.get("format") != "source_fragments_v2":
                    v1_membership[link.provision_id].append((chunk_ordinals[link.chunk_id], link.ordinal))
            for pid in interleaved_parents:
                parent_order = min(v1_membership.get(pid, [(10**9, 10**9)]))
                children = [p for p in provisions if p.parent_id == pid]
                if any(parent_order < min(v1_membership.get(child.id, [(10**9, 10**9)]))
                       and any(a[0] == b[0] and a[1:] > b[1:]
                               for a in positions[pid] for b in positions[child.id])
                       for child in children):
                    flag("confirmed_v1_parent_continuation_misorder", str(pid))
            excluded_cache = {}
            def excluded(p):
                if p.id in excluded_cache:
                    return excluded_cache[p.id]
                own = p.kind == "other" or p.source_status in {"omitted", "repealed", "deleted"}
                ancestor = provision_by_id.get(p.parent_id)
                value = own or (excluded(ancestor) if ancestor is not None else False)
                excluded_cache[p.id] = value
                return value
            for row in sets:
                if row.chunk_report.get("format") != "source_fragments_v2":
                    continue
                for p in provisions:
                    if p.revision_id != row.revision_id or p.kind in {"part", "chapter"} or \
                            excluded(p) or not p.own_text.strip():
                        continue
                    if (row.id, p.id) not in membership:
                        flag("v2_eligible_provision_not_chunked", f"{row.id}:{p.id}")
            v2_links = defaultdict(list)
            for link in links:
                if link.source_span_id is not None:
                    v2_links[(link.chunk_set_id, link.provision_id)].append(
                        (link.provision_char_start, link.provision_char_end))
            for (set_id, pid), ranges in v2_links.items():
                source = provision_by_id[pid].own_text
                covered = [False] * len(source)
                for start, end in ranges:
                    for index in range(start, end):
                        if covered[index]:
                            flag("v2_overlapping_provision_coverage", f"{set_id}:{pid}:{index}")
                            break
                        covered[index] = True
                if any(not covered[index] and not char.isspace()
                       for index, char in enumerate(source)):
                    flag("v2_missing_provision_coverage", f"{set_id}:{pid}")
            return {
                "approved_revisions": len(revisions),
                "provisions": len(provisions), "chunk_sets": len(sets),
                "chunks": len(chunks), "links": len(links),
                "exact_source_span_mismatches": span_issues.bad_exact,
                "v2_search_stats": dict(search_stats),
                "v2_duplicate_text_groups": duplicate_text_groups,
                "instrument_alias_stats": dict(alias_stats),
                "v2_chunks_with_repeated_provision_fragments": repeated_v2_membership_chunks,
                "keyword_indexes": {row.indexname: row.indexdef for row in indexes},
                "source_interleaved_parent_patterns": len(interleaved_parents),
                "by_chunker_version": {
                    version: {"sets": sum(s.chunker_version == version for s in sets),
                              "chunks": sum(set_by_id[c.chunk_set_id].chunker_version == version
                                            for c in chunks)}
                    for version in sorted({s.chunker_version for s in sets})},
                "issues": dict(issues), "samples": dict(samples),
                "revisions": [{"title": r.canonical_title, "edition": r.edition_key,
                    "revision_id": str(r.id), "revision_number": r.revision_number,
                    "chunk_set_ids": [str(s.id) for s in sets if s.revision_id == r.id]}
                    for r in revisions],
            }
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision-id", type=UUID)
    parser.add_argument("--summary", action="store_true", help="Omit the per-revision list.")
    parser.add_argument("--gate", action="store_true", help="Exit 2 if v2 integrity/search checks fail.")
    args = parser.parse_args()
    for attempt in range(3):
        try:
            result = asyncio.run(audit(args.revision_id))
            break
        except DBAPIError as exc:
            if not exc.connection_invalidated or attempt == 2:
                raise
    if args.summary:
        result.pop("revisions")
    print(json.dumps(result, indent=2, default=str))
    if args.gate and (
        any(code.startswith("v2_") or code in {"chunk_without_provisions",
                                                   "stale_or_unapproved_chunk", "wrong_chunk_hash",
                                                   "wrong_char_count", "cross_revision_membership",
                                                   "set_report_count_mismatch"}
            for code in result["issues"])
        or result["exact_source_span_mismatches"]
        or result["v2_search_stats"]["bad_vectors"]
        or result["v2_search_stats"]["empty_chunks"]
        or result["v2_search_stats"]["excluded_links"]
        or result["v2_search_stats"]["missing_provision_hash_links"]
        or "ix_legal_chunks_search_vector" not in result["keyword_indexes"]
    ):
        raise SystemExit(2)
