"""Non-streaming OpenAI-compatible question generation through 9Router."""

import asyncio
import json
import logging
from typing import NoReturn

import httpx
from pydantic import BaseModel

from app.config import Settings
from app.providers.base import ProviderError, ProviderTimeoutError
from app.schemas.grounded_generation import RawGroundedQuestionList
from app.schemas.question import RawQuestionList

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 1_048_576
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}


class _RetryableStatus(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def _strict_objects(node: object) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        for child in node.values():
            _strict_objects(child)
    elif isinstance(node, list):
        for child in node:
            _strict_objects(child)


def _invalid_response(reason: str) -> NoReturn:
    logger.warning("nine_router_invalid_response reason=%s", reason)
    raise ProviderError("9Router returned an invalid completion")


class NineRouterProvider:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._closed = False
        try:
            self._client = httpx.AsyncClient(
                base_url=settings.router_base_url.rstrip("/") + "/",
                headers={"Authorization": f"Bearer {settings.router_api_key}", "Accept": "application/json"},
                timeout=settings.router_timeout_ms / 1000,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            )
        except Exception as exc:
            logger.warning("nine_router_failure stage=initialize exception_type=%s", type(exc).__name__)
            raise ProviderError("9Router client initialization failed") from None

    async def generate(self, prompt: str) -> str:
        """Generate text drafts; the shared generator validates slots and answers."""
        return await self._generate(prompt, RawQuestionList)

    async def generate_grounded(self, prompt: str) -> str:
        """Generate grounded/planned drafts without changing citation validation."""
        return await self._generate(prompt, RawGroundedQuestionList)

    async def _generate(self, prompt: str, schema_model: type[BaseModel]) -> str:
        schema = schema_model.model_json_schema()
        _strict_objects(schema)
        system = "Return only a JSON object. Follow all requested slot and source constraints."
        if self._settings.router_response_format == "json_schema":
            response_format = {"type": "json_schema", "json_schema": {
                "name": schema_model.__name__, "strict": True, "schema": schema,
            }}
        else:
            response_format = {"type": "json_object"}
            system += " Match this JSON schema: " + json.dumps(schema)
        payload = {
            "model": self._settings.router_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "stream": False,
            "n": 1,
            "max_tokens": self._settings.router_max_output_tokens,
            "response_format": response_format,
        }
        for attempt in range(self._settings.router_max_attempts):
            try:
                return await self._request(payload)
            except _RetryableStatus as exc:
                error = (ProviderTimeoutError("9Router request timed out") if exc.status in (408, 504)
                         else ProviderError("9Router request failed"))
            except (TimeoutError, httpx.TransportError) as exc:
                logger.warning("nine_router_failure stage=generate exception_type=%s", type(exc).__name__)
                error = (ProviderTimeoutError("9Router request timed out") if isinstance(exc, (TimeoutError, httpx.TimeoutException))
                         else ProviderError("9Router request failed"))
            except ProviderError:
                raise
            except Exception as exc:
                logger.warning("nine_router_failure stage=generate exception_type=%s", type(exc).__name__)
                raise ProviderError("9Router request failed") from None
            if attempt + 1 == self._settings.router_max_attempts:
                raise error from None
            # Retries and backoff remain inside QuestionGenerator's shared deadline.
            await asyncio.sleep(min(0.5 * 2**attempt, 2))
        raise ProviderError("9Router request failed")

    async def _request(self, payload: dict[str, object]) -> str:
        async with self._client.stream("POST", "chat/completions", json=payload) as response:
            if response.status_code != 200:
                logger.warning("nine_router_failure stage=generate status_code=%s", response.status_code)
                if response.status_code in RETRYABLE_STATUSES:
                    raise _RetryableStatus(response.status_code)
                raise ProviderError("9Router request failed")
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                _invalid_response("content_type")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    _invalid_response("body_limit")
                body.extend(chunk)
        try:
            envelope: object = json.loads(body)
        except (ValueError, UnicodeError):
            _invalid_response("json")
        if not isinstance(envelope, dict):
            _invalid_response("envelope")
        choices = envelope.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            _invalid_response("choices")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            _invalid_response("finish_reason")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("refusal") or message.get("tool_calls"):
            _invalid_response("message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            _invalid_response("content")
        return content

    async def aclose(self) -> None:
        """Close the shared HTTP pool once, without exposing upstream diagnostics."""
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.wait_for(self._client.aclose(), timeout=5)
        except Exception as exc:
            logger.warning("nine_router_failure stage=close exception_type=%s", type(exc).__name__)
