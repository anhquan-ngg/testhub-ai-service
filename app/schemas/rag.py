"""RAG contracts. Scope comes only from the authenticated NestJS caller."""

import hashlib
import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.document import ExtractionResult

DIMENSIONS = 768
BATCH_SIZE = 16
MAX_CHUNK_BYTES = 1800
TARGET_CHUNK_BYTES = 1200
OVERLAP_BYTES = 200
MAX_CHUNKS = 5000
MAX_CONTEXT_BYTES = 12_000
Uuid = Annotated[UUID, Field(strict=False)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RagErrorDetail(StrictModel):
    code: str
    retryable: bool


class RagErrorResponse(StrictModel):
    detail: RagErrorDetail


class IndexProfile(StrictModel):
    profile_id: Hash
    config: dict[str, str | int]


def current_profile() -> IndexProfile:
    """Freeze model/task/dimension/chunk behavior into an immutable profile hash."""
    config = {
        "model": "gemini-embedding-001", "dimensions": DIMENSIONS,
        "document_task": "RETRIEVAL_DOCUMENT", "query_task": "RETRIEVAL_QUERY",
        "vector_normalization": "l2-v1", "text_normalization": "none-v1",
        "chunk_algorithm": "utf8-paragraph-window-v1", "source_offsets": "unicode-codepoints-v1",
        "target_bytes": TARGET_CHUNK_BYTES, "max_bytes": MAX_CHUNK_BYTES,
        "overlap_bytes": OVERLAP_BYTES,
    }
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return IndexProfile(profile_id=digest, config=config)


class IndexScope(StrictModel):
    owner_id: Uuid
    topic_id: Uuid
    chapter_id: Uuid
    document_id: Uuid
    version_id: Uuid


class IndexCommand(StrictModel):
    index_id: Uuid
    profile_id: Hash
    scope: IndexScope


class PrepareIndexRequest(IndexCommand):
    extraction: ExtractionResult


class IndexProgress(StrictModel):
    index_id: Uuid
    profile_id: Hash
    status: Literal["BUILDING", "READY", "REVOKED"]
    chunk_count: int = Field(ge=1, le=MAX_CHUNKS)
    completed_chunks: int = Field(ge=0, le=MAX_CHUNKS)


    @model_validator(mode="after")
    def consistent_progress(self) -> Self:
        if self.completed_chunks > self.chunk_count or (self.status == "READY" and self.completed_chunks != self.chunk_count):
            raise ValueError("Invalid index progress")
        return self


class Chunk(StrictModel):
    chunk_id: Uuid
    ordinal: int = Field(ge=0, lt=MAX_CHUNKS)
    kind: Literal["PAGE", "SLIDE", "NOTES"]
    position: int = Field(ge=1, le=500)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)
    text: str = Field(min_length=1)
    text_sha256: Hash


    @model_validator(mode="after")
    def source_integrity(self) -> Self:
        if len(self.text.encode()) > MAX_CHUNK_BYTES or self.end_offset-self.start_offset != len(self.text) or hashlib.sha256(self.text.encode()).hexdigest() != self.text_sha256:
            raise ValueError("Invalid source chunk")
        return self


class RetrieveRequest(StrictModel):
    owner_id: Uuid
    profile_id: Hash
    index_ids: list[Uuid] = Field(min_length=1, max_length=10)
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=6, ge=1, le=8)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        """Bound query bytes without silently truncating Vietnamese text."""
        value = value.strip()
        if not value or len(value.encode("utf-8")) > MAX_CHUNK_BYTES:
            raise ValueError("Query exceeds embedding input budget")
        return value

    @field_validator("index_ids")
    @classmethod
    def unique_indexes(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Duplicate index scope")
        return value


class RetrievedChunk(Chunk):
    index_id: Uuid
    document_id: Uuid
    version_id: Uuid
    source_sha256: Hash
    similarity: float = Field(ge=-1.000001, le=1.000001, allow_inf_nan=False)


class RetrieveResponse(StrictModel):
    profile_id: Hash
    quality_status: Literal["UNASSESSED"] = "UNASSESSED"
    context_bytes: int = Field(ge=1, le=MAX_CONTEXT_BYTES)
    chunks: list[RetrievedChunk] = Field(min_length=1, max_length=8)
