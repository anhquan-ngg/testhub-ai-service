"""rag-generation.v1: validated source evidence accompanies every generated draft."""
from typing import Literal, Self
from pydantic import BaseModel, Field, model_validator
from app.schemas.question import RawQuestion, RawQuestionList, GeneratedQuestion, QuestionFormat, GeminiQuestion
from app.schemas.rag import RetrieveRequest, RetrievedChunk, Uuid, StrictModel, Hash

class GroundedGenerateRequest(RetrieveRequest):
    question_format: QuestionFormat = Field(strict=False)
    count: int = Field(ge=1, le=20)

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
    citations: list[GeminiCitation]

class GeminiGroundedQuestionList(BaseModel):
    questions: list[GeminiGroundedQuestion]
