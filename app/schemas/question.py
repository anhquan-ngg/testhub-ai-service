"""Request and response contracts aligned with TestHub's question model."""

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class QuestionFormat(str, Enum):
    KNOWLEDGE = "KNOWLEDGE"
    UNDERSTANDING = "UNDERSTANDING"
    APPLYING = "APPLYING"
    ADVANCED = "ADVANCED"


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=20, max_length=20_000)
    topic: str | None = Field(default=None, min_length=1, max_length=200)
    question_format: QuestionFormat
    count: Annotated[StrictInt, Field(ge=1, le=20)]

    @field_validator("topic", mode="before")
    @classmethod
    def normalize_topic(cls, value: object) -> object:
        """Treat an omitted or blank optional topic consistently."""
        if isinstance(value, str):
            return value.strip() or None
        return value


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    text: str = Field(min_length=1, max_length=500)
    isCorrect: bool


class RawQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    question_text: str = Field(min_length=1, max_length=2_000)
    options: list[QuestionOption] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def check_options(self) -> "RawQuestion":
        if sum(option.isCorrect for option in self.options) != 1:
            raise ValueError("A question must have exactly one correct option")
        normalized = [option.text.casefold() for option in self.options]
        if len(set(normalized)) != len(normalized):
            raise ValueError("A question must have four distinct options")
        return self


class RawQuestionList(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    questions: list[RawQuestion]

    @model_validator(mode="after")
    def check_duplicates(self) -> "RawQuestionList":
        normalized = [question.question_text.casefold() for question in self.questions]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Question texts must be distinct")
        return self


class GeneratedQuestion(RawQuestion):
    question_type: Literal["SINGLE_CHOICE"] = "SINGLE_CHOICE"
    question_format: QuestionFormat
    correct_answer: None = None


class GenerateResponse(BaseModel):
    questions: list[GeneratedQuestion]


# A deliberately simple shape for Gemini's structured-output schema. The
# stricter RawQuestionList validators run on the returned JSON afterward.
class GeminiOption(BaseModel):
    text: str
    isCorrect: bool


class GeminiQuestion(BaseModel):
    question_text: str
    options: list[GeminiOption]


class GeminiQuestionList(BaseModel):
    questions: list[GeminiQuestion]
