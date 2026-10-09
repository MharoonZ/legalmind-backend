"""Input for local-only draft structure creation."""

from uuid import UUID

from pydantic import BaseModel, Field


class StructureCreateIn(BaseModel):
    # Omit to select the latest completed extraction for every PDF assigned
    # to this edition. Explicit IDs support multi-PDF/manual source selection.
    extraction_ids: list[UUID] | None = None


class ApprovalDecisionIn(BaseModel):
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=10, max_length=2000)
    acknowledge_findings: bool = False
