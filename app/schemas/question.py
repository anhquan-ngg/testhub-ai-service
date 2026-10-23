"""Request and response contracts aligned with TestHub's question model."""

from enum import Enum
from typing import Annotated, NamedTuple, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

MAX_TOTAL_QUESTIONS = 20
MAX_QUESTION_CONFIGS = 10


class QuestionFormat(str, Enum):
    KNOWLEDGE = "KNOWLEDGE"
    UNDERSTANDING = "UNDERSTANDING"
    APPLYING = "APPLYING"
    ADVANCED = "ADVANCED"


class QuestionType(str, Enum):
    SINGLE_CHOICE = "SINGLE_CHOICE"
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"


def answers_match(question_type: QuestionType, options: list["QuestionOption"]) -> bool:
    """SINGLE_CHOICE has one correct option; MULTIPLE_CHOICE has two or more, never all."""
    correct = sum(option.isCorrect for option in options)
    if question_type is QuestionType.SINGLE_CHOICE:
        return correct == 1
    return 2 <= correct < len(options)


class QuestionConfig(BaseModel):
    """One requested row: question type, cognitive level and number of questions."""

    model_config = ConfigDict(extra="forbid")

    question_type: QuestionType = Field(strict=False)
    question_format: QuestionFormat = Field(strict=False)
    count: Annotated[StrictInt, Field(ge=1, le=MAX_TOTAL_QUESTIONS)]


class QuestionSlot(NamedTuple):
    question_type: QuestionType
    question_format: QuestionFormat


class QuestionBlueprint(BaseModel):
    """Accept legacy single-choice question_format/count or question_configs, never both."""

    question_format: QuestionFormat | None = Field(default=None, strict=False)
    count: StrictInt | None = Field(default=None, ge=1, le=MAX_TOTAL_QUESTIONS)
    question_configs: list[QuestionConfig] | None = Field(
        default=None, min_length=1, max_length=MAX_QUESTION_CONFIGS,
    )

    @model_validator(mode="after")
    def normalize_blueprint(self) -> Self:
        legacy = self.question_format is not None or self.count is not None
        if self.question_configs is None:
            if self.question_format is None or self.count is None:
                raise ValueError("Provide question_configs or question_format with count")
            self.question_configs = [QuestionConfig(
                question_type=QuestionType.SINGLE_CHOICE,
                question_format=self.question_format, count=self.count,
            )]
        elif legacy:
            raise ValueError("question_configs cannot be combined with question_format/count")
        if sum(config.count for config in self.question_configs) > MAX_TOTAL_QUESTIONS:
            raise ValueError(f"At most {MAX_TOTAL_QUESTIONS} questions can be requested")
        return self

    def slots(self) -> list[QuestionSlot]:
        """Expand configs into the ordered type/level expected for every question."""
        return [
            QuestionSlot(config.question_type, config.question_format)
            for config in self.question_configs or []
            for _ in range(config.count)
        ]


class GenerateRequest(QuestionBlueprint):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=20, max_length=20_000)
    topic: str | None = Field(default=None, min_length=1, max_length=200)

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

    # The required number of correct options depends on the requested slot type;
    # QuestionGenerator checks it position by position.
    @model_validator(mode="after")
    def check_options(self) -> "RawQuestion":
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
    question_type: QuestionType = QuestionType.SINGLE_CHOICE
    question_format: QuestionFormat
    correct_answer: None = None

    @model_validator(mode="after")
    def check_answers(self) -> Self:
        if not answers_match(self.question_type, self.options):
            raise ValueError("Correct options do not match the question type")
        return self


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
