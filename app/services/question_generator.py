"""Validate provider output and convert it to TestHub question drafts."""

import asyncio
import logging

from pydantic import ValidationError

from app.errors import ServiceError, ServiceUnavailableError
from app.providers.base import ProviderTimeoutError, QuestionProvider
from app.schemas.question import (
    GenerateRequest,
    GenerateResponse,
    GeneratedQuestion,
    RawQuestionList,
)
from app.services.prompt_builder import build_prompt
from app.schemas.grounded_generation import GroundedGenerateRequest, GroundedGenerateResponse
from app.schemas.rag import RetrieveResponse

logger = logging.getLogger(__name__)


class InvalidQuestionsError(ServiceError):
    """The provider returned JSON that cannot be accepted as question drafts."""


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
        if len(parsed.questions) != request.count:
            logger.warning("generation_invalid_output reason=count")
            raise InvalidQuestionsError("Gemini returned the wrong question count")
        # Source fields have already passed RawQuestionList validation, and the
        # format comes from a validated request. Avoid validating them again.
        return GenerateResponse.model_construct(
            questions=[
                GeneratedQuestion.model_construct(
                    question_text=question.question_text,
                    options=question.options,
                    question_format=request.question_format,
                )
                for question in parsed.questions
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
                raise InvalidQuestionsError("Invalid grounded questions") from None
            if len(parsed.questions)!=request.count:
                raise InvalidQuestionsError("Insufficient grounded questions")
            by_id={c.chunk_id:c for c in context.chunks}
            questions=[]
            for question in parsed.questions:
                citations=[]
                if len({c.chunk_id for c in question.citations})!=len(question.citations):
                    raise InvalidQuestionsError("Duplicate citations")
                for citation in question.citations:
                    chunk=by_id.get(citation.chunk_id)
                    if chunk is None or not citation.quote.strip() or citation.quote not in chunk.text:
                        raise InvalidQuestionsError("Unsupported citation evidence")
                    citations.append(SourceCitation(chunk=chunk,quote=citation.quote))
                questions.append(GroundedQuestion(question_text=question.question_text,options=question.options,
                    question_format=request.question_format,citations=citations))
            return GroundedGenerateResponse(profile_id=context.profile_id,questions=questions)
        finally:
            self._semaphore.release()
