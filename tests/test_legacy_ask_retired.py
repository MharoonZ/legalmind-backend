"""The old 30-section search API must not query retired tables."""

from app.db import legal_corpus_models, models  # noqa: F401 - register metadata
from app.db.base import Base
from app.main import create_app


def test_legacy_search_routes_are_not_exposed() -> None:
    paths = set(create_app().openapi()["paths"])
    assert "/ask" not in paths
    assert "/health/retrieval" not in paths
    assert "/health" in paths
    assert "/contracts" in paths


def test_legacy_search_tables_are_not_in_current_metadata() -> None:
    assert not {"law_sources", "law_sections", "law_passages"} & set(Base.metadata.tables)
    assert {"legal_files", "corpus_revisions", "provisions", "documents"} <= set(Base.metadata.tables)
