"""Read-only corpus inventory, relationship checks, role grants and query plans."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import Database

# Rows of the active publication, joined exactly as a published chunk is defined.
SCOPE = """
 FROM legal_chunks c
 JOIN publication_items pi ON pi.chunk_set_id=c.chunk_set_id AND pi.revision_id=c.revision_id
 JOIN corpus_publications pub ON pub.id=pi.publication_id AND pub.status='active'
 JOIN legal_chunk_embeddings e ON e.chunk_id=c.id AND e.revision_id=c.revision_id
   AND e.profile_id=pub.profile_id
 WHERE pub.id=:publication_id
"""

TABLES = """
    alembic_version app_users documents document_versions contract_pages contract_clauses contract_clause_spans
    legal_files legal_file_assignments legal_instruments legal_versions file_extractions file_pages file_blocks
    corpus_revisions revision_sources provisions provision_spans revision_block_coverage provision_references
    instrument_relations review_events processing_jobs job_attempts chunk_sets legal_chunks legal_chunk_provisions
    embedding_profiles legal_chunk_embeddings corpus_publications publication_items qa_runs retrieval_hits
    answer_claims answer_evidence
""".split()

CHECKS = {
    "completed_extractions_with_page_gaps": """
        SELECT count(*) FROM file_extractions e JOIN legal_files f ON f.id=e.file_id
        LEFT JOIN LATERAL (
            SELECT count(*) AS n, min(page_index) AS first, max(page_index) AS last
            FROM file_pages WHERE extraction_id=e.id
        ) p ON true
        WHERE e.processing_status='completed' AND
            (p.n<>f.page_count OR p.first IS DISTINCT FROM 1 OR p.last IS DISTINCT FROM f.page_count)
    """,
    "cross_revision_or_orphan_parents": """
        SELECT count(*) FROM provisions p LEFT JOIN provisions parent ON parent.id=p.parent_id
        WHERE p.parent_id IS NOT NULL AND parent.revision_id IS DISTINCT FROM p.revision_id
    """,
    "duplicate_sibling_labels": """
        SELECT count(*) FROM (SELECT revision_id,parent_id,kind,lower(label)
            FROM provisions WHERE label IS NOT NULL AND kind NOT IN ('other')
            GROUP BY revision_id,parent_id,kind,lower(label) HAVING count(*) > 1) q
    """,
    "exact_span_text_or_identity_mismatch": """
        SELECT count(*) FROM provision_spans s
        LEFT JOIN provisions p ON p.id=s.provision_id AND p.revision_id=s.revision_id
        LEFT JOIN file_blocks b ON b.id=s.block_id AND b.page_id=s.page_id
        LEFT JOIN file_pages pg ON pg.id=s.page_id AND pg.extraction_id=s.extraction_id
        WHERE s.mapping_quality='exact' AND (p.id IS NULL OR b.id IS NULL OR pg.id IS NULL
            OR substring(p.own_text from s.provision_char_start+1 for s.provision_char_end-s.provision_char_start)
            IS DISTINCT FROM
            substring(b.normalized_text from s.block_char_start+1 for s.block_char_end-s.block_char_start))
    """,
    "chunk_links_with_wrong_identity": """
        SELECT count(*) FROM legal_chunk_provisions l
        LEFT JOIN legal_chunks c ON c.id=l.chunk_id AND c.revision_id=l.revision_id
        LEFT JOIN provisions p ON p.id=l.provision_id AND p.revision_id=l.revision_id
        WHERE c.id IS NULL OR p.id IS NULL
    """,
    "chunk_groups_with_bad_sequence": """
        SELECT count(*) FROM (
            SELECT chunk_set_id, root_provision_id FROM legal_chunks GROUP BY chunk_set_id, root_provision_id
            HAVING count(*) <> max(root_total_chunks) OR min(root_total_chunks) <> max(root_total_chunks)
                OR min(sequence_in_root) <> 1 OR max(sequence_in_root) <> count(*)
                OR count(DISTINCT sequence_in_root) <> count(*)
        ) q
    """,
    "published_instruments_with_unsupported_applicability": """
        SELECT count(*) FROM publication_items pi JOIN corpus_publications p ON p.id=pi.publication_id
        JOIN legal_versions v ON v.id=pi.legal_version_id
        WHERE p.status='active' AND v.applicability_status='unknown'
    """,
}


def plan_summary(raw):
    plan = json.loads(raw) if isinstance(raw, str) else raw
    row = plan[0]
    nodes = [row["Plan"]]
    scans = []
    while nodes:
        node = nodes.pop()
        if "Scan" in node["Node Type"]:
            scans.append({key: node[key] for key in ("Node Type", "Relation Name", "Index Name", "Actual Rows")
                          if key in node})
        nodes.extend(node.get("Plans", []))
    return {"planning_ms": row["Planning Time"], "execution_ms": row["Execution Time"], "scans": scans}


async def audit():
    db = Database(get_settings())
    try:
        async with db.session() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            async def rows(sql, params=None):
                return [dict(row) for row in (await session.execute(text(sql), params)).mappings()]

            tables = await rows("""
                SELECT c.relname AS table_name, c.relrowsecurity AS rls,
                       c.relforcerowsecurity AS force_rls, pg_table_is_visible(c.oid) AS visible,
                       (SELECT count(*) FROM pg_policy p WHERE p.polrelid=c.oid) AS policies,
                       (SELECT count(*) FROM pg_constraint k WHERE k.conrelid=c.oid AND k.contype='f') AS foreign_keys,
                       (SELECT count(*) FROM pg_constraint k
                          WHERE k.conrelid=c.oid AND k.contype='f' AND NOT k.convalidated) AS unvalidated_foreign_keys,
                       (SELECT count(*) FROM pg_index i WHERE i.indrelid=c.oid AND i.indisvalid) AS valid_indexes
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[])) AND c.relkind IN ('r','p')
                ORDER BY c.relname
            """, {"tables": TABLES})
            present = {row["table_name"] for row in tables}
            # Identifiers originate exclusively in the fixed allow-list above.
            counts = await rows(" UNION ALL ".join(
                f"SELECT '{table}' AS table_name, count(*) AS rows FROM public.{table}"
                for table in TABLES if table in present))
            grants = await rows("""
                SELECT r.rolname AS role, c.relname AS table_name,
                       has_table_privilege(r.oid,c.oid,'SELECT') AS can_select,
                       has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE') AS can_write
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                CROSS JOIN pg_roles r
                WHERE n.nspname='public' AND c.relname=ANY(CAST(:tables AS text[]))
                    AND r.rolname IN ('anon','authenticated','service_role')
                ORDER BY r.rolname, c.relname
            """, {"tables": TABLES})
            report = {
                "alembic": await rows("SELECT version_num FROM alembic_version"),
                "connection_role": (await rows("""
                    SELECT rolsuper AS superuser, rolbypassrls AS bypass_rls
                    FROM pg_roles WHERE rolname=current_user
                """))[0],
                "tables": tables, "row_counts": counts, "client_role_grants": grants,
                "missing_tables": sorted(set(TABLES) - present),
                "checks": (await rows("SELECT " + ", ".join(f"({sql}) AS {name}"
                                                         for name, sql in CHECKS.items())))[0],
                "revision_states": await rows("SELECT review_state, count(*) FROM corpus_revisions GROUP BY review_state"),
                "page_outcomes": await rows("SELECT outcome, count(*) FROM file_pages GROUP BY outcome"),
                "publications": await rows("SELECT id,name,status,chunk_count FROM corpus_publications ORDER BY created_at"),
            }
            publication = next((row for row in report["publications"] if row["status"] == "active"), None)
            if publication:
                params = {"publication_id": publication["id"]}
                report["active_chunk_inventory"] = (await rows(
                    "SELECT count(DISTINCT c.root_provision_id) AS roots, count(*) AS chunks, "
                    "count(DISTINCT c.root_provision_id) FILTER (WHERE c.root_total_chunks>1) AS split_roots, "
                    "max(c.root_total_chunks) AS most_fragments, max(c.char_count) AS max_characters " + SCOPE,
                    params))[0]
                explain = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                lexical = await session.scalar(text(explain + "SELECT c.id " + SCOPE +
                    " AND c.search_vector @@ plainto_tsquery('english',:query) LIMIT 50"),
                    {**params, "query": "receipt purchaser"})
                vector = await session.scalar(text(explain + "SELECT c.id " + SCOPE +
                    " ORDER BY e.embedding <=> (SELECT embedding FROM legal_chunk_embeddings "
                    "ORDER BY chunk_id LIMIT 1) LIMIT 50"), params)
                report["plans"] = {"lexical": plan_summary(lexical), "vector": plan_summary(vector)}
            return report
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(audit())
    except Exception as exc:
        raise SystemExit(f"Database audit could not complete ({type(exc).__name__}).") from None
    args.output.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")
    print("Wrote database audit to", args.output)
