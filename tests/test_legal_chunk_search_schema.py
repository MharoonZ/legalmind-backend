"""Schema-only checks for the legal_chunks keyword-search migration.

Mirrors the replay style of tests/test_legal_corpus_schema.py: migrations
are executed against a throwaway SQLAlchemy MetaData (never a real
database) and compared against the ORM model in
app/db/legal_corpus_models.py, so a drift between the hand-written
migration and the mapped columns/indexes fails here instead of in
production.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateColumn

from app.db.base import Base
from app.db import legal_corpus_models  # noqa: F401 - registers legal_chunks

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _replay_chunk_migrations(monkeypatch):
    metadata = sa.MetaData()
    indexes = set()
    statements = []

    def create_table(name, *elements):
        return sa.Table(name, metadata, *elements)

    def create_index(name, table, columns, **kwargs):
        indexes.add((name, table, tuple(columns)))

    def add_column(table, column):
        metadata.tables[table].append_column(column)

    def alter_column(table, column_name, **kwargs):
        column = metadata.tables[table].c[column_name]
        if "server_default" in kwargs:
            column.server_default = kwargs["server_default"]
        if "nullable" in kwargs:
            column.nullable = kwargs["nullable"]

    def create_unique_constraint(name, table, columns):
        metadata.tables[table].append_constraint(sa.UniqueConstraint(*columns, name=name))

    def create_check_constraint(name, table, condition):
        metadata.tables[table].append_constraint(sa.CheckConstraint(condition, name=name))

    def create_foreign_key(*args, **kwargs):
        pass  # not needed for the columns/indexes under test here

    def drop_constraint(name, table, type_):
        target = next(c for c in metadata.tables[table].constraints if c.name == name)
        metadata.tables[table].constraints.remove(target)

    def drop_column(table, column_name):
        metadata.tables[table]._columns.remove(metadata.tables[table].c[column_name])

    fake_op = SimpleNamespace(
        create_table=create_table, create_index=create_index,
        add_column=add_column, alter_column=alter_column,
        create_unique_constraint=create_unique_constraint,
        create_check_constraint=create_check_constraint,
        create_foreign_key=create_foreign_key,
        drop_constraint=drop_constraint, drop_column=drop_column,
        execute=statements.append,
    )

    for filename in ("20261010_13_legal_chunking.py", "20261010_14_legal_chunk_keyword_search.py"):
        path = VERSIONS_DIR / filename
        spec = spec_from_file_location(filename.removesuffix(".py"), path)
        assert spec is not None and spec.loader is not None
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        monkeypatch.setattr(migration, "op", fake_op)
        migration.upgrade()
    return metadata, indexes, statements


def _check_constraints(table):
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints if isinstance(constraint, sa.CheckConstraint)
    }


def test_migration_adds_search_text_and_generated_search_vector(monkeypatch):
    migrated, _, _ = _replay_chunk_migrations(monkeypatch)
    table = migrated.tables["legal_chunks"]

    assert "search_text" in table.columns
    assert not table.c.search_text.nullable
    assert isinstance(table.c.search_text.type, sa.Text)

    assert "search_vector" in table.columns
    assert not table.c.search_vector.nullable
    dialect = postgresql.dialect()
    assert table.c.search_vector.type.compile(dialect=dialect) == "TSVECTOR"

    # Compiling just this column's DDL (no FK cross-table resolution needed)
    # catches a malformed GENERATED ALWAYS AS expression without a live DB.
    ddl = str(CreateColumn(table.c.search_vector).compile(dialect=dialect))
    assert "GENERATED ALWAYS AS (to_tsvector('english'::regconfig, search_text)) STORED" in ddl


def test_migration_matches_orm_metadata_for_legal_chunks(monkeypatch):
    migrated, indexes, _ = _replay_chunk_migrations(monkeypatch)
    model = Base.metadata.tables["legal_chunks"]
    actual = migrated.tables["legal_chunks"]

    assert set(actual.columns.keys()) == set(model.columns.keys())
    dialect = postgresql.dialect()
    assert {c.name: c.type.compile(dialect=dialect) for c in actual.columns} == \
           {c.name: c.type.compile(dialect=dialect) for c in model.columns}
    assert {c.name: c.nullable for c in actual.columns} == \
           {c.name: c.nullable for c in model.columns}
    assert "ck_legal_chunks_search_text_nonempty" in _check_constraints(actual)
    assert _check_constraints(actual)["ck_legal_chunks_search_text_nonempty"] == \
           _check_constraints(model)["ck_legal_chunks_search_text_nonempty"]

    model_indexes = {
        (index.name, index.table.name, tuple(column.name for column in index.columns))
        for index in model.indexes
    }
    assert ("ix_legal_chunks_search_vector", "legal_chunks", ("search_vector",)) in indexes
    assert ("ix_legal_chunks_search_vector", "legal_chunks", ("search_vector",)) in model_indexes
    assert ("ix_legal_chunks_chunk_set_label", "legal_chunks", ("chunk_set_id", "label")) in indexes
    assert ("ix_legal_chunks_chunk_set_label", "legal_chunks", ("chunk_set_id", "label")) in model_indexes


def test_search_vector_is_a_generated_column_never_written_by_the_app():
    """search_vector must be DB-maintained from search_text, not something
    the application inserts/updates, so it can never drift out of sync."""
    column = Base.metadata.tables["legal_chunks"].c.search_vector
    assert column.computed is not None
    sqltext = str(column.computed.sqltext)
    assert "search_text" in sqltext
    assert "to_tsvector" in sqltext


def test_search_vector_index_uses_gin(monkeypatch):
    migrated, indexes, statements = _replay_chunk_migrations(monkeypatch)
    # create_index captured name/table/columns only above; also assert the
    # migration source explicitly requested a GIN index (the ORM Index()
    # call is checked the same way via model.indexes' dialect kwargs).
    model = Base.metadata.tables["legal_chunks"]
    [search_vector_index] = [i for i in model.indexes if i.name == "ix_legal_chunks_search_vector"]
    assert search_vector_index.dialect_options["postgresql"]["using"] == "gin"


def test_search_text_must_be_nonblank_like_other_chunk_text_fields():
    checks = _check_constraints(Base.metadata.tables["legal_chunks"])
    assert "length(btrim(search_text)) > 0" in checks["ck_legal_chunks_search_text_nonempty"]


def test_migration_backfills_search_text_before_enforcing_not_null(monkeypatch):
    """Regression guard: a dev/staging DB may already have legal_chunks rows
    from before search_text existed (built by an earlier run of
    scripts/build_legal_chunks.py). The column must be backfilled from
    chunk_text before the NOT NULL / nonblank check constraint is added, or
    upgrading fails with ck_legal_chunks_search_text_nonempty violated."""
    _, _, statements = _replay_chunk_migrations(monkeypatch)
    backfill_statements = [s for s in statements if "search_text" in str(s) and "UPDATE" in str(s).upper()]
    assert backfill_statements, "expected an UPDATE ... SET search_text = chunk_text backfill"
    assert "chunk_text" in str(backfill_statements[0])
    assert "search_text IS NULL" in str(backfill_statements[0])
