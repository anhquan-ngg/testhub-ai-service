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
