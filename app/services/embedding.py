"""Independent Google embedding adapter; shares GEMINI_API_KEY, not LLM retries."""

import asyncio
import logging
import math
from typing import Literal, Protocol

import httpx
from google import genai
from google.genai import errors, types

from app.schemas.rag import BATCH_SIZE, DIMENSIONS, MAX_CHUNK_BYTES
from app.services.rag_errors import RagError

logger = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str], task: Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]) -> list[list[float]]: ...
    async def aclose(self) -> None: ...


def normalized_vector(values: list[float]) -> list[float]:
    """Reject malformed, nonfinite, zero or wrong-size vectors before persistence."""
    if len(values) != DIMENSIONS or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in values):
        raise RagError("RAG_EMBEDDING_INVALID", 502)
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm <= 0:
        raise RagError("RAG_EMBEDDING_INVALID", 502)
    return [value / norm for value in values]


class GoogleEmbeddingProvider:
    def __init__(self, api_key: str) -> None:
        try:
            self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
                timeout=25_000, retry_options=types.HttpRetryOptions(attempts=1),
            ))
        except Exception:
            raise RagError("RAG_EMBEDDING_UNAVAILABLE", 503) from None

    async def embed(self, texts: list[str], task: Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]) -> list[list[float]]:
        """Use explicit document/query tasks and normalize 768-dimensional outputs."""
        if not 1 <= len(texts) <= BATCH_SIZE or any(not s.strip() or len(s.encode()) > MAX_CHUNK_BYTES for s in texts):
            raise RagError("RAG_EMBEDDING_INPUT_INVALID")
        try:
            response = await asyncio.wait_for(self._client.aio.models.embed_content(
                model="gemini-embedding-001", contents=texts,
                config=types.EmbedContentConfig(task_type=task, output_dimensionality=DIMENSIONS),
            ), timeout=30)
            if not response.embeddings or len(response.embeddings) != len(texts):
                raise RagError("RAG_EMBEDDING_INVALID", 502)
            return [normalized_vector(item.values or []) for item in response.embeddings]
        except RagError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise RagError("RAG_EMBEDDING_TIMEOUT", 504, True) from None
        except Exception as exc:
            status = exc.code if isinstance(exc, errors.APIError) else None
            logger.warning("embedding_failed exception_type=%s status=%s", type(exc).__name__, status)
            if status in (400, 401, 403, 404):
                raise RagError("RAG_EMBEDDING_CONFIG", 502) from None
            raise RagError("RAG_EMBEDDING_UNAVAILABLE", 503, True) from None

    async def aclose(self) -> None:
        """Close independently without letting SDK diagnostics escape shutdown."""
        try:
            await asyncio.wait_for(self._client.aio.aclose(), 5)
        except Exception:
            logger.warning("embedding_async_close_failed")
        try:
            self._client.close()
        except Exception:
            logger.warning("embedding_sync_close_failed")
