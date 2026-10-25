"""Read-only Steps 10-13 + source-span smoke check; no LLM or audit writes.

Run from the backend root:
    .\\.venv\\Scripts\\python.exe -m scripts.demo_phase_c_readonly
"""
import asyncio

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.legal_search_repository import LegalSearchRepository
from app.repositories.qa_context_repository import QaContextRepository
from app.schemas.legal_search import LegalSearchIn
from app.services.legal_retrieval import LegalRetrievalService
from app.services.qa_context import ContextExpander
from app.services.qa_evidence_gate import EvidenceGate
from app.services.qa_preparation import QuestionPreparationService
from app.services.qa_reranker import CandidateReranker
from app.services.qa_source_verification import QaSourceVerifier


async def main():
    settings = get_settings()
    db = Database(settings)
    try:
        service = QuestionPreparationService(
            LegalRetrievalService(LegalSearchRepository(db), settings),
            CandidateReranker(), ContextExpander(QaContextRepository(db)), EvidenceGate())
        decision = await service.prepare(LegalSearchIn(
            query="Section 19 of the Consumer Protection Act", limit=10))
        print("Evidence decision:", decision.outcome)
        if decision.outcome != "ready_for_prompt":
            raise SystemExit("The evidence gate did not approve this smoke-test question.")
        if decision.context:
            print("First-stage candidates:", len(decision.context.reranked.retrieval.results))
            print("Reranked candidates:", len(decision.context.reranked.candidates))
            print("Context passages:", len(decision.context.units))
            print("Context issues:", [issue.code for issue in decision.context.issues])
            for unit in decision.context.units:
                if unit.source_kind != "chunk":
                    continue
                for ref in unit.source_refs:
                    start, end = ref.get("chunk_char_start"), ref.get("chunk_char_end")
                    if start is None or end is None or end - start < 20:
                        continue
                    end = min(end, start + 180)
                    proof = await QaSourceVerifier(db).verify(unit, start, end)
                    print("PDF source-span proof:", "passed" if proof else "missing")
                    if not proof:
                        raise SystemExit("Source-span verification failed.")
                    return
            raise SystemExit("PDF source-span proof: no qualifying chunk fragment")
    except Exception as exc:
        print("Read-only Phase C smoke check failed:", type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
