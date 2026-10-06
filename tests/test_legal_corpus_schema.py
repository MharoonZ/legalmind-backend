"""Schema-only checks for Haroon's additive ingestion and structure migrations.

These tests never connect to the real database. PostgreSQL execution and the
cross-row approval gate still need a disposable database integration test.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.db.base import Base
from app.db import legal_corpus_models  # noqa: F401 - registers the new tables
from app.db import models  # noqa: F401 - registers the legacy tables


HAROON_TABLES = {
    "app_users", "jurisdictions", "categories", "legal_instruments",
    "instrument_categories", "instrument_relations", "legal_versions",
    "legal_files", "legal_file_assignments", "file_extractions", "file_pages", "file_blocks",
    "corpus_revisions", "revision_sources", "provisions", "provision_spans",
    "provision_references", "revision_block_coverage", "review_events", "processing_jobs", "job_attempts",
}


def _migration_schema(monkeypatch):
    metadata = sa.MetaData()
    indexes = set()
    statements = []

    def create_table(name, *elements):
        return sa.Table(name, metadata, *elements)

    def create_index(name, table, columns):
        indexes.add((name, table, tuple(columns)))

    def add_column(table, column):
        metadata.tables[table].append_column(column)

    for filename in ("20261005_05_haroon_legal_corpus.py", "20261006_06_structure_coverage.py"):
        path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / filename
        spec = spec_from_file_location(filename.removesuffix(".py"), path)
        assert spec is not None and spec.loader is not None
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        monkeypatch.setattr(migration, "op", SimpleNamespace(
            create_table=create_table, create_index=create_index,
            add_column=add_column, execute=statements.append,
        ))
        migration.upgrade()
    return metadata, indexes, statements


def _constraint_names(table):
    return {constraint.name for constraint in table.constraints if constraint.name is not None}


def _check_constraints(table):
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints if isinstance(constraint, sa.CheckConstraint)
    }


def _foreign_keys(table):
    return {
        (tuple(element.parent.name for element in constraint.elements),
         tuple(element.target_fullname for element in constraint.elements))
        for constraint in table.foreign_key_constraints
    }


def test_haroon_migration_matches_orm_metadata(monkeypatch):
    migrated, indexes, statements = _migration_schema(monkeypatch)
    assert set(migrated.tables) == HAROON_TABLES

    for name in HAROON_TABLES:
        model = Base.metadata.tables[name]
        actual = migrated.tables[name]
        assert set(actual.columns.keys()) == set(model.columns.keys()), name
        assert {c.name for c in actual.primary_key} == {c.name for c in model.primary_key}, name
        assert {c.name: c.nullable for c in actual.columns} == {c.name: c.nullable for c in model.columns}, name
        dialect = postgresql.dialect()
        assert {c.name: c.type.compile(dialect=dialect) for c in actual.columns} == {c.name: c.type.compile(dialect=dialect) for c in model.columns}, name
        assert {c.name: str(c.server_default.arg) if c.server_default is not None else None for c in actual.columns} == {c.name: str(c.server_default.arg) if c.server_default is not None else None for c in model.columns}, name
        assert _constraint_names(actual) == _constraint_names(model), name
        assert _check_constraints(actual) == _check_constraints(model), name
        assert _foreign_keys(actual) == _foreign_keys(model), name
        # A PostgreSQL dialect compilation also catches malformed column/FK DDL.
        str(CreateTable(actual).compile(dialect=postgresql.dialect()))

    model_indexes = {
        (index.name, index.table.name, tuple(column.name for column in index.columns))
        for table in Base.metadata.tables.values() if table.name in HAROON_TABLES
        for index in table.indexes
    }
    assert indexes == model_indexes
    assert any("CREATE TRIGGER tr_review_events_append_only" in statement for statement in statements)


def test_spans_cannot_mix_revisions_extractions_pages_or_blocks():
    table = Base.metadata.tables["provision_spans"]
    foreign_keys = _foreign_keys(table)
    assert (("provision_id", "revision_id"), ("provisions.id", "provisions.revision_id")) in foreign_keys
    assert (("revision_id", "extraction_id"), ("revision_sources.revision_id", "revision_sources.extraction_id")) in foreign_keys
    assert (("page_id", "extraction_id"), ("file_pages.id", "file_pages.extraction_id")) in foreign_keys
    assert (("block_id", "page_id", "extraction_id"), ("file_blocks.id", "file_blocks.page_id", "file_blocks.extraction_id")) in foreign_keys


def test_block_coverage_is_scoped_to_selected_revision_source():
    foreign_keys = _foreign_keys(Base.metadata.tables["revision_block_coverage"])
    assert (("revision_id", "extraction_id"), ("revision_sources.revision_id", "revision_sources.extraction_id")) in foreign_keys
    assert (("block_id", "page_id", "extraction_id"), ("file_blocks.id", "file_blocks.page_id", "file_blocks.extraction_id")) in foreign_keys


def test_revision_source_matches_upload_assignment_and_extracted_file():
    foreign_keys = _foreign_keys(Base.metadata.tables["revision_sources"])
    assert (("revision_id", "legal_version_id"), ("corpus_revisions.id", "corpus_revisions.legal_version_id")) in foreign_keys
    assert (("extraction_id", "file_id"), ("file_extractions.id", "file_extractions.file_id")) in foreign_keys
    assert (("file_id", "legal_version_id"), ("legal_file_assignments.file_id", "legal_file_assignments.legal_version_id")) in foreign_keys


def test_haroon_schema_does_not_reference_legacy_or_private_contract_tables():
    for name in HAROON_TABLES:
        for constraint in Base.metadata.tables[name].foreign_key_constraints:
            for element in constraint.elements:
                assert element.column.table.name in HAROON_TABLES


def test_provision_label_is_text_and_not_limited_to_thirty():
    provisions = Base.metadata.tables["provisions"]
    assert isinstance(provisions.c.label.type, sa.Text)
    assert isinstance(provisions.c.reference_path.type, sa.Text)
    assert not any("30" in str(c.sqltext) for c in provisions.constraints if isinstance(c, sa.CheckConstraint))
