"""Input for local-only draft structure creation."""

from uuid import UUID

from pydantic import BaseModel


class StructureCreateIn(BaseModel):
    # Omit to select the latest completed extraction for every PDF assigned
    # to this edition. Explicit IDs support multi-PDF/manual source selection.
    extraction_ids: list[UUID] | None = None
