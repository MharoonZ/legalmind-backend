"""Expected errors for legal-PDF ingestion and contract uploads."""


class LegalMindError(Exception):
    """Base class for expected, handled failures."""


class DocumentValidationError(LegalMindError):
    """An uploaded contract failed validation (bad type, too big, too many
    pages, encrypted, unreadable, or no selectable text). The message is
    safe to show the user as-is.
    """


class DocumentStorageError(LegalMindError):
    """An uploaded contract could not be saved to disk."""


class LegalUploadValidationError(LegalMindError):
    """A legal-corpus PDF or its registration metadata is invalid."""


class LegalUploadConflictError(LegalMindError):
    """A registry key already exists with different metadata."""


class LegalUploadStorageError(LegalMindError):
    """A legal-corpus PDF could not be stored safely."""


class ContractExtractionError(LegalMindError):
    """A stored contract could not be converted into pages and clauses."""


class ContractCitationError(LegalMindError):
    """A contract citation does not match its document, clause, quote, or pages."""
