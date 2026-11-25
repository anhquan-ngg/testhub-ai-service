"""Content-map and reviewed matrix contracts; no bank chapter IDs cross this boundary."""
from typing import Literal, Self
from pydantic import Field, model_validator
from app.schemas.rag import StrictModel, Uuid, Hash, SourceUnit
from app.schemas.question import QuestionConfig
from app.schemas.document import MAX_UNITS, OcrPage


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

class EvidenceCheckItem(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    source_units: list[SourceUnit] = Field(min_length=1, max_length=1000)
    question_type: Literal["SINGLE_CHOICE", "MULTIPLE_CHOICE"] = "SINGLE_CHOICE"
    question_format: Literal["KNOWLEDGE", "UNDERSTANDING", "APPLYING", "ADVANCED"] = "KNOWLEDGE"

class EvidenceCheckRequest(StrictModel):
    owner_id: Uuid
    index_id: Uuid
    profile_id: Hash
    items: list[EvidenceCheckItem] = Field(min_length=1, max_length=200)
    ocr_candidate_units: list[int] = Field(default_factory=list, max_length=MAX_UNITS)
    ocr_pages: list[OcrPage] = Field(default_factory=list, max_length=MAX_UNITS)

    @model_validator(mode="after")
    def unique_evidence_metadata(self) -> Self:
        if (len({item.id for item in self.items}) != len(self.items)
                or len(set(self.ocr_candidate_units)) != len(self.ocr_candidate_units)
                or any(type(p) is not int or not 1 <= p <= MAX_UNITS for p in self.ocr_candidate_units)
                or len({page.position for page in self.ocr_pages}) != len(self.ocr_pages)):
            raise ValueError("Duplicate or invalid evidence metadata")
        return self

class EvidenceCheckResult(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    status: Literal["OK", "THIN_TEXT", "NO_TEXT", "TITLE_ONLY", "OCR_INCOMPLETE", "INSUFFICIENT_DEPTH", "SOURCE_MISSING"]
    body_chars: int = Field(ge=0)
    unit_count: int = Field(ge=0)
    substantive_chars: int = Field(ge=0)
    fact_count: int = Field(ge=0)

class EvidenceCheckResponse(StrictModel):
    contract_version: Literal["knowledge-evidence-check.v1"] = "knowledge-evidence-check.v1"
    index_id: Uuid
    items: list[EvidenceCheckResult] = Field(max_length=200)
