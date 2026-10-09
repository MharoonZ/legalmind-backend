# Legacy 30-section search retirement — 9 October 2026

Migration `20261009_12` dropped only `law_passages`, `law_sections`, and
`law_sources` from the development PostgreSQL database, in dependency order.
No `CASCADE` was used. The retired tables held 41, 30, and 1 rows respectively.

Before the migration, their full rows (including the embedding/search fields)
were exported to the local, git-ignored file
`uploads/legacy-backups/legacy_law_tables_2026-10-09.json`.
SHA-256: `f84f91cca0c8c33126148457b4b80ce570674f3b3b0c959781372536d686e7b5`.
Keep this file if historical restoration might be required. Downgrading the
migration recreates the schema only; it does not automatically reload rows.

The old `/ask` and `/health/retrieval` routes are no longer registered.
The home page no longer invites users to call the retired endpoint. The old
search implementation files remain as historical code, but are not mounted and
their ORM models are excluded from current Alembic metadata. Do not run old
`ingest_contract_act_corpus.py`, `build_embeddings.py`, or `approve_source.py`
against this schema.

Post-migration checks found 29 public tables and no legacy trio. The new
legal-PDF corpus still had 23 `legal_files`, 28 `file_extractions`, 241
`file_pages`, 1,912 `file_blocks`, 87 `corpus_revisions`, 12,268 `provisions`,
and 16,992 `provision_spans`. The separate private-contract tables still held
2 `documents`, 2 `document_versions`, and 4 `contract_pages`.

The new multi-document question-answering API has not been implemented. It
must query approved revisions and future publication/chunk tables, not the
retired 30-section schema.
