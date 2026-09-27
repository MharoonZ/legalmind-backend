"""Errors the /ask pipeline can raise.

Each one maps to a specific HTTP response in `app.api.routes.ask`, so the
frontend can tell "the Act does not cover this" (a normal 200 answer with
`supported: false`) apart from "our service is broken" (503).
"""


class LegalMindError(Exception):
    """Base class for expected, handled failures."""


class RetrievalError(LegalMindError):
    """The corpus could not be searched (database down, bad SQL, timeout)."""


class EmbeddingError(LegalMindError):
    """The sentence-transformers model is missing or failed to encode."""


class LLMError(LegalMindError):
    """OpenAI was unreachable, timed out, or returned unusable output."""


class DocumentValidationError(LegalMindError):
    """An uploaded contract failed validation (bad type, too big, too many
    pages, encrypted, unreadable, or no selectable text). The message is
    safe to show the user as-is.
    """


class DocumentStorageError(LegalMindError):
    """An uploaded contract could not be saved to disk."""


class ContractExtractionError(LegalMindError):
    """A stored contract could not be converted into pages and clauses."""


class ContractCitationError(LegalMindError):
    """A contract citation does not match its document, clause, quote, or pages."""
