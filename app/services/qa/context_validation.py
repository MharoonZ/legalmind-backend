"""Source and context integrity checks independent of relevance."""
import re
from uuid import UUID

from app.schemas.qa_context import ContextResult


def _source_identity(ref):
    if ref.get("mapping_quality") != "exact":
        return False
    page = ref.get("page_index")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("file_sha256", ""))):
        return False
    try:
        for field in ("source_span_id", "file_id", "block_id"):
            UUID(str(ref.get(field)))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _scope(item):
    return item.legal_version_id, item.revision_id, item.chunk_set_id


def validate_context(context: ContextResult) -> None:
    """Validate immutable source identities and complete passage budgets."""
    response = context.reranked.retrieval
    if response.status != "ok" or response.publication_id is None or not context.units:
        raise ValueError("Published source context is unavailable.")
    if any(issue.critical for issue in context.issues):
        raise ValueError("Required source context is incomplete.")
    if any(unit.publication_id != response.publication_id or not unit.source_refs or
           not unit.text.strip() or not all(_source_identity(ref) for ref in unit.source_refs)
           for unit in context.units):
        raise ValueError("A context passage lacks exact source provenance.")

    if len({unit.evidence_id for unit in context.units}) != len(context.units):
        raise ValueError("Context contains duplicate evidence identities.")
    scope_by_law, retained = {}, {}
    for unit in context.units:
        identity = unit.chunk_id if unit.source_kind == "chunk" else unit.provision_id
        if identity is None or unit.evidence_id != f"{unit.source_kind}:{identity}":
            raise ValueError("A context evidence identity is inconsistent.")
        scope = _scope(unit)
        if unit.instrument_id in scope_by_law and scope_by_law[unit.instrument_id] != scope:
            raise ValueError("Context mixes source versions for one law.")
        scope_by_law[unit.instrument_id] = scope
        if unit.relation == "retrieved":
            retained[unit.chunk_id] = unit
    selected = context.reranked.candidates
    if len({row.candidate.chunk_id for row in selected}) != len(selected) or set(retained) != {
            row.candidate.chunk_id for row in selected}:
        raise ValueError("Selected candidates and retained evidence do not agree.")
    for row in selected:
        candidate = row.candidate
        unit = retained[candidate.chunk_id]
        if (row.publication_id != response.publication_id or unit.source_kind != "chunk" or
                unit.instrument_id != candidate.instrument_id or _scope(unit) != _scope(candidate) or
                unit.provision_id != candidate.root_provision_id or unit.text != candidate.chunk_text or
                candidate.exact_rank is not None and candidate.exact_rank < 1):
            raise ValueError("A retained passage differs from its selected source identity.")


    actual_chars = sum(len(unit.text) for unit in context.units)
    if (actual_chars != context.char_count or actual_chars > context.char_limit
            or len(context.units) > context.unit_limit
            or not 0 <= context.token_count <= context.token_limit):
        raise ValueError("Complete source context exceeds its budget.")
