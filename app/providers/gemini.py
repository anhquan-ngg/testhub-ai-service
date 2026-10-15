"""Gemini implementation of the question provider contract."""

import asyncio
import logging

import httpx
from google import genai
from google.genai import errors, types

from app.config import Settings
from app.providers.base import ProviderError, ProviderTimeoutError
from app.schemas.question import GeminiQuestionList

logger = logging.getLogger(__name__)


def log_failure(stage: str, exc: Exception) -> None:
    """Log diagnostic metadata without exception messages or tracebacks."""
    code = exc.code if isinstance(exc, errors.APIError) else None
    logger.warning(
        "gemini_failure stage=%s exception_type=%s status_code=%s",
        stage, type(exc).__name__, code,
    )


class GeminiProvider:
    def __init__(self, settings: Settings) -> None:
        self.model = settings.gemini_model
        self._closed = False
        try:
            self._client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(
                    timeout=settings.gemini_timeout_ms,
                    retry_options=types.HttpRetryOptions(
                        attempts=settings.gemini_max_attempts,
                        initial_delay=0.5,
                        max_delay=2,
                        exp_base=2,
                        jitter=0.5,
                        http_status_codes=[408, 429, 500, 502, 503, 504],
                    ),
                ),
            )
        except Exception as exc:
            log_failure("initialize", exc)
            raise ProviderError("Gemini client initialization failed") from None

    async def generate(self, prompt: str) -> str:
        """Generate structured JSON using the application's shared SDK client."""
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=GeminiQuestionList,
                ),
            )
            if not response.text:
                logger.warning("gemini_empty_response")
                raise ProviderError("Gemini returned no text")
            return response.text
        except (TimeoutError, httpx.TimeoutException) as exc:
            log_failure("generate", exc)
            raise ProviderTimeoutError("Gemini request timed out") from None
        except ProviderError:
            raise
        except Exception as exc:
            log_failure("generate", exc)
            if isinstance(exc, errors.APIError) and exc.code in (408, 504):
                raise ProviderTimeoutError("Gemini request timed out") from None
            raise ProviderError("Gemini request failed") from None

    async def aclose(self) -> None:
        """Close both transports independently; cleanup errors stay sanitized."""
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.wait_for(self._client.aio.aclose(), timeout=5)
        except Exception as exc:
            log_failure("async_close", exc)
        try:
            self._client.close()
        except Exception as exc:
            log_failure("sync_close", exc)
