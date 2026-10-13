"""Revision-safe V2 fragment-ledger schema regression checks."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import ForeignKeyConstraint

from app.db.base import Base
from app.db import legal_corpus_models  # noqa: F401 - register models
from app.repositories.legal_chunk_repository import LegalChunkRepository


def test_fragment_rows_allow_repeated_parent_within_one_chunk():
    table = Base.metadata.tables["legal_chunk_provisions"]
    assert {column.name for column in table.primary_key.columns} == {"chunk_id", "ordinal"}
    assert {"source_span_id", "provision_char_start", "provision_char_end",
            "chunk_char_start", "chunk_char_end"} <= set(table.columns.keys())
    assert any(
        {column.name for column in fk.columns} ==
        {"source_span_id", "provision_id", "revision_id"}
        and fk.referred_table.name == "provision_spans"
        for fk in table.constraints if isinstance(fk, ForeignKeyConstraint)
    )


def test_empty_queries_cannot_turn_into_unbounded_searches():
    repo = LegalChunkRepository(None)
    assert asyncio.run(repo.keyword_search(uuid4(), "  ")) == []
    assert asyncio.run(repo.keyword_search_revisions([uuid4()], "  ")) == []
    assert asyncio.run(repo.keyword_search_revisions([], "law")) == []
    with pytest.raises(ValueError, match="between 1 and 100"):
        asyncio.run(repo.keyword_search_revisions([uuid4()], "law", limit=101))
