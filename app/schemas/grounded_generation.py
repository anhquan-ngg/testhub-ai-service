"""rag-generation.v1: validated source evidence accompanies every generated draft."""
from typing import Literal, Self
from pydantic import BaseModel, Field, model_validator
from app.schemas.question import RawQuestion, RawQuestionList, GeneratedQuestion, QuestionBlueprint, GeminiQuestion
from app.schemas.rag import RetrieveRequest, RetrievedChunk, Uuid, StrictModel, Hash
from app.schemas.knowledge_plan import PlanItem

class GroundedGenerateRequest(RetrieveRequest, QuestionBlueprint):
    """Retrieval scope plus legacy question_format/count or ordered question_configs."""
    plan_items: list[PlanItem] | None = Field(default=None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def planned_slots(self) -> Self:
        if self.plan_items:
            from app.schemas.question import QuestionSlot
            slots = [QuestionSlot(item.question_type, item.question_format)
                     for item in self.plan_items for _ in range(item.count)]
            if slots != self.slots() or len({item.id for item in self.plan_items}) != len(self.plan_items):
                raise ValueError("Plan does not match requested slots")
            if len(self.index_ids) != 1 or self.source_units is not None:
                raise ValueError("A plan pins one immutable document and per-item source units")
        return self

class RawCitation(StrictModel):
    chunk_id: Uuid
    quote: str = Field(min_length=1, max_length=600)

class RawGroundedQuestion(RawQuestion):
    citations: list[RawCitation] = Field(min_length=1, max_length=3)

class RawGroundedQuestionList(RawQuestionList):
    questions: list[RawGroundedQuestion] = Field(min_length=1, max_length=20)

class SourceCitation(StrictModel):
    chunk: RetrievedChunk
    quote: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def evidence(self) -> Self:
        if not self.quote.strip() or self.quote not in self.chunk.text:
            raise ValueError("Invalid evidence quote")
        return self

class GroundedQuestion(GeneratedQuestion):
    citations: list[SourceCitation] = Field(min_length=1, max_length=3)
    plan_item_id: str | None = Field(default=None, min_length=1, max_length=100)

class GroundedGenerateResponse(StrictModel):
    contract_version: Literal["rag-generation.v1"] = "rag-generation.v1"
    profile_id: Hash
    quality_status: Literal["UNASSESSED"] = "UNASSESSED"
    questions: list[GroundedQuestion] = Field(min_length=1, max_length=20)

# Simple SDK schema; strict validators and quote checks run after generation.
class GeminiCitation(BaseModel):
    chunk_id: str
    quote: str

class GeminiGroundedQuestion(GeminiQuestion):
    citations: list[GeminiCitation] = Field(min_length=1, max_length=3,
        description="One citation per distinct chunk_id. Combine supporting facts from the same chunk into one exact contiguous quote of at most 600 characters.")

class GeminiGroundedQuestionList(BaseModel):
    questions: list[GeminiGroundedQuestion]
