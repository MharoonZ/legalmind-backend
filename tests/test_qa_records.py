"""Phase C audit persistence is tested with an isolated fake session only."""
import asyncio
from contextlib import asynccontextmanager

from app.db.base import Base
from app.db.qa_record_models import AnswerClaim, AnswerEvidence, QaRun, RetrievalHit
from app.repositories.qa_record_repository import QaRecordRepository
from app.schemas.qa import AskIn
from tests.test_qa_answer import answer, ready


class FakeSession:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    @asynccontextmanager
    async def begin(self):
        yield self


class FakeDB:
    def __init__(self):
        self.session_instance = FakeSession()

    @asynccontextmanager
    async def session(self):
        yield self.session_instance


def test_phase_c_audit_writes_only_its_four_tables_in_one_session():
    decision, draft = ready()
    response, _, _ = answer(decision, [draft])
    db = FakeDB()
    asyncio.run(QaRecordRepository(db, "synthetic-model").record(
        AskIn(question=response.question), response, decision))
    rows = db.session_instance.rows
    assert sum(isinstance(row, QaRun) for row in rows) == 1
    assert sum(isinstance(row, RetrievalHit) for row in rows) == 1
    assert sum(isinstance(row, AnswerClaim) for row in rows) == 1
    assert sum(isinstance(row, AnswerEvidence) for row in rows) == 1
    assert rows[-1].source_span_id == response.citations[0].source_span_id
    assert {"qa_runs", "retrieval_hits", "answer_claims", "answer_evidence"} <= set(Base.metadata.tables)
