"""Validate provider output and convert it to TestHub question drafts."""

import asyncio
import logging
import unicodedata

from pydantic import ValidationError

from app.errors import ServiceError, ServiceUnavailableError
from app.providers.base import ProviderTimeoutError, QuestionProvider
from app.schemas.question import (
    GenerateRequest,
    GenerateResponse,
    GeneratedQuestion,
    QuestionSlot,
    RawQuestion,
    RawQuestionList,
    answers_match,
)
from app.services.prompt_builder import build_prompt
from app.schemas.grounded_generation import GroundedGenerateRequest, GroundedGenerateResponse
from app.schemas.rag import RetrieveResponse

logger = logging.getLogger(__name__)


class InvalidQuestionsError(ServiceError):
    """The provider returned JSON that cannot be accepted as question drafts."""


def _comparable(text: str) -> tuple[str, list[int]]:
    """NFD-decompose per character and collapse whitespace, mapping back to source indexes."""
    chars: list[str] = []
    origins: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            if chars and chars[-1] != " ":
                chars.append(" ")
                origins.append(index)
            continue
        for part in unicodedata.normalize("NFD", char):
            chars.append(part)
            origins.append(index)
    return "".join(chars), origins


def locate_quote(quote: str, text: str) -> str | None:
    """Return the exact source span matching a quote despite whitespace/Unicode form drift."""
    if quote.strip() and quote in text:
        return quote
    needle = _comparable(quote)[0].strip()
    haystack, origins = _comparable(text)
    start = haystack.find(needle) if needle else -1
    if start < 0:
        return None
    end = start + len(needle) - 1
    # Reject matches that start or end inside a decomposed source character (e.g. "a" of "á").
    if (start > 0 and origins[start - 1] == origins[start]) or (
        end + 1 < len(origins) and origins[end + 1] == origins[end]
    ):
        return None
    return text[origins[start]:origins[end] + 1]


def check_slots(questions: list[RawQuestion], slots: list[QuestionSlot]) -> None:
    """Require one question per requested slot with that slot's correct-option rule."""
    if len(questions) != len(slots):
        logger.warning("generation_invalid_output reason=count")
        raise InvalidQuestionsError("Gemini returned the wrong question count")
    if not all(answers_match(slot.question_type, question.options)
               for question, slot in zip(questions, slots)):
        logger.warning("generation_invalid_output reason=answers")
        raise InvalidQuestionsError("Gemini returned answers that do not match the question type")


class QuestionGenerator:
    def __init__(
        self, provider: QuestionProvider, *, timeout_ms: int = 30_000,
        max_concurrency: int = 4,
    ) -> None:
        self.provider = provider
        self._timeout_seconds = timeout_ms / 1000
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        """Limit total time and concurrency, including queueing and SDK retries."""
        try:
            return await asyncio.wait_for(
                self._generate_with_slot(request), timeout=self._timeout_seconds,
            )
        except TimeoutError:
            logger.warning("generation_deadline_exceeded")
            raise ProviderTimeoutError("Generation deadline exceeded") from None

    async def _generate_with_slot(self, request: GenerateRequest) -> GenerateResponse:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=1)
        except TimeoutError:
            raise ServiceUnavailableError("Generation capacity reached") from None
        try:
            return await self._generate(request)
        finally:
            self._semaphore.release()

    async def _generate(self, request: GenerateRequest) -> GenerateResponse:
        raw_text = await self.provider.generate(build_prompt(request))
        try:
            parsed = RawQuestionList.model_validate_json(raw_text)
        except ValidationError:
            logger.warning("generation_invalid_output reason=schema")
            raise InvalidQuestionsError("Gemini returned invalid question JSON") from None
        slots = request.slots()
        check_slots(parsed.questions, slots)
        # Source fields and answer counts have already been validated, and the
        # type/format come from a validated request. Avoid validating them again.
        return GenerateResponse.model_construct(
            questions=[
                GeneratedQuestion.model_construct(
                    question_text=question.question_text,
                    options=question.options,
                    question_type=slot.question_type,
                    question_format=slot.question_format,
                    correct_answer=None,
                )
                for question, slot in zip(parsed.questions, slots)
            ]
        )

    async def generate_grounded(self, request: GroundedGenerateRequest, context: RetrieveResponse) -> GroundedGenerateResponse:
        """Share generation admission/deadline and reject unsupported citation evidence."""
        try:
            return await asyncio.wait_for(self._grounded_with_slot(request,context),self._timeout_seconds)
        except TimeoutError:
            raise ProviderTimeoutError("Generation deadline exceeded") from None

    async def _grounded_with_slot(self, request: GroundedGenerateRequest, context: RetrieveResponse) -> GroundedGenerateResponse:
        from app.schemas.grounded_generation import RawGroundedQuestionList, GroundedQuestion, GroundedGenerateResponse, SourceCitation
        from app.services.grounded_generation import grounded_prompt
        try:
            await asyncio.wait_for(self._semaphore.acquire(),1)
        except TimeoutError:
            raise ServiceUnavailableError("Generation capacity reached") from None
        try:
            raw = await self.provider.generate_grounded(grounded_prompt(request,[c.model_dump(mode="json") for c in context.chunks]))
            try:
                parsed=RawGroundedQuestionList.model_validate_json(raw)
            except ValidationError:
                logger.warning("generation_invalid_output reason=schema")
                raise InvalidQuestionsError("Invalid grounded questions") from None
            slots=request.slots()
            check_slots(parsed.questions,slots)
            by_id={c.chunk_id:c for c in context.chunks}
            questions=[]
            for question,slot in zip(parsed.questions,slots):
                citations=[]
                if len({c.chunk_id for c in question.citations})!=len(question.citations):
                    logger.warning("generation_invalid_output reason=duplicate_citations")
                    raise InvalidQuestionsError("Duplicate citations")
                for citation in question.citations:
                    chunk=by_id.get(citation.chunk_id)
                    if chunk is None:
                        logger.warning("generation_invalid_output reason=unknown_chunk")
                        raise InvalidQuestionsError("Unsupported citation evidence")
                    quote=locate_quote(citation.quote,chunk.text)
                    if quote is None or len(quote)>600:
                        logger.warning("generation_invalid_output reason=quote_mismatch")
                        raise InvalidQuestionsError("Unsupported citation evidence")
                    citations.append(SourceCitation(chunk=chunk,quote=quote))
                questions.append(GroundedQuestion(question_text=question.question_text,options=question.options,
                    question_type=slot.question_type,question_format=slot.question_format,citations=citations))
            return GroundedGenerateResponse(profile_id=context.profile_id,questions=questions)
        finally:
            self._semaphore.release()
