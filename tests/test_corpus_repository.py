"""The importer must never publish or overwrite unreviewed evidence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.corpus.models import CorpusVersion
from app.corpus.repository import CorpusRepository
from app.db.models import LawSource


def load_corpus() -> CorpusVersion:
    path = Path(__file__).resolve().parents[1] / "data" / "contract_act_1872_sections_1_30.json"
    return CorpusVersion.from_dict(json.loads(path.read_text(encoding="utf-8")))


class CaptureSession:
    """Only the session methods used on the insert path are needed here."""

    def __init__(self, existing: LawSource | None = None) -> None:
        self.existing = existing
        self.added: LawSource | None = None

    async def get(self, _model, _key):
        return self.existing

    def add(self, source: LawSource) -> None:
        self.added = source


def test_new_import_keeps_evidence_and_review_state() -> None:
    corpus = load_corpus()
    session = CaptureSession()

    inserted = asyncio.run(CorpusRepository(session).import_version(corpus))

    assert inserted is True
    assert session.added is not None
    assert session.added.review_status == "needs_review"
    assert len(session.added.sections) == 30
    assert sum(len(section.passages) for section in session.added.sections) == 41
    assert session.added.sections[9].passages[0].evidence_id == corpus.sections[9].passages[0].evidence_id
    assert all(
        passage.embedding is None and passage.embedding_model is None
        for section in session.added.sections
        for passage in section.passages
    )


def test_existing_id_with_conflicting_provenance_is_rejected() -> None:
    corpus = load_corpus()
    existing = LawSource(
        id=corpus.source_id,
        law_name=corpus.law_name,
        jurisdiction=corpus.jurisdiction,
        source_url="https://different.example/pdf",
        pdf_sha256=corpus.pdf_sha256,
        corpus_sha256=corpus.corpus_sha256,
        pdf_page_count=corpus.pdf_page_count,
        review_status="needs_review",
    )
    with pytest.raises(ValueError, match="conflicting provenance"):
        asyncio.run(CorpusRepository(CaptureSession(existing)).import_version(corpus))
