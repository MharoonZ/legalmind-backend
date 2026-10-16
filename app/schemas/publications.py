from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class PublicationCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160)
    chunk_set_ids: list[UUID] = Field(min_length=1, max_length=1000)
    notes: str | None = Field(default=None, max_length=4000)


class PublicationActivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
