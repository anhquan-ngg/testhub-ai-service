"""Content-map and reviewed matrix contracts; no bank chapter IDs cross this boundary."""
from typing import Literal, Self
from pydantic import Field, model_validator
from app.schemas.rag import StrictModel, Uuid, Hash, SourceUnit
from app.schemas.question import QuestionConfig


class OutlineRequest(StrictModel):
    owner_id: Uuid
    index_id: Uuid
    profile_id: Hash
    title: str = Field(min_length=1, max_length=200)


class OutlineNode(StrictModel):
    id: Uuid
    parent_id: Uuid | None
    name: str = Field(min_length=1, max_length=200)
    source_units: list[SourceUnit] = Field(min_length=1, max_length=1000)
    origin: Literal["HEADING", "INFERRED"]
    evidence: str = Field(min_length=1, max_length=200)


class OutlineResponse(StrictModel):
    contract_version: Literal["knowledge-outline.v1"] = "knowledge-outline.v1"
    index_id: Uuid
    document_id: Uuid
    version_id: Uuid
    source_sha256: Hash
    nodes: list[OutlineNode] = Field(min_length=1, max_length=1000)
    source_units: list[SourceUnit] = Field(min_length=1, max_length=1000)
    warnings: list[str] = Field(max_length=10)


class PlanItem(QuestionConfig):
    id: str = Field(min_length=1, max_length=100)
    knowledge_title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=400)
    source_units: list[SourceUnit] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_units(self) -> Self:
        if len({(unit.kind, unit.position) for unit in self.source_units}) != len(self.source_units):
            raise ValueError("Duplicate source unit")
        return self
