"""API authentication and access to application-scoped services."""

import secrets

from fastapi import Header, HTTPException, Request

from app.config import Settings
from app.errors import ServiceUnavailableError
from app.services.question_generator import QuestionGenerator
from app.services.rag_errors import RagError


def require_internal_token(
    request: Request, x_internal_token: str | None = Header(default=None),
) -> None:
    """Compare encoded bytes so non-ASCII input cannot cause a TypeError."""
    settings: Settings = request.app.state.settings
    if not settings.internal_api_token:
        if request.url.path.startswith("/internal/rag/"):
            raise RagError("RAG_AUTH_UNAVAILABLE", 503)
        raise HTTPException(503, "Internal token not configured")
    if not x_internal_token or not secrets.compare_digest(
        x_internal_token.encode("utf-8"), settings.internal_api_token.encode("utf-8"),
    ):
        if request.url.path.startswith("/internal/rag/"):
            raise RagError("RAG_UNAUTHORIZED", 401)
        raise HTTPException(401, "Unauthorized")


def get_generator(request: Request) -> QuestionGenerator:
    """Return the shared service after startup, or report unavailable config."""
    generator: QuestionGenerator | None = request.app.state.generator
    if generator is None:
        raise ServiceUnavailableError("Gemini API not configured or unavailable")
    return generator
