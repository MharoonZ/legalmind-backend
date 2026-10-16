import asyncio

import pytest
from sqlalchemy.exc import DBAPIError

from app.repositories.publication_repository import PublicationRepository
from app.services.corpus_validation import PublicationValidationError


def test_concurrent_publication_reports_busy_before_locking_or_modifying_corpus():
    class Session:
        statements = []
        async def execute(self, statement): self.statements.append(str(statement))
        async def scalar(self, statement, params):
            assert "current_schema()" in str(statement)
            return False
    session = Session()
    with pytest.raises(PublicationValidationError, match="in progress"):
        asyncio.run(PublicationRepository._lock(session))
    assert not any("LOCK TABLE" in statement for statement in session.statements)


def test_busy_ingestion_lock_is_a_publication_conflict_instead_of_an_unhandled_database_error():
    class LockUnavailable(Exception): sqlstate = "55P03"
    class Session:
        async def execute(self, statement):
            if "LOCK TABLE" in str(statement):
                raise DBAPIError(str(statement), {}, LockUnavailable())
        async def scalar(self, *args): return True
    with pytest.raises(PublicationValidationError, match="writers are busy"):
        asyncio.run(PublicationRepository._lock(Session()))
