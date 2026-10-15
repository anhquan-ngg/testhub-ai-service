"""Central mapping from expected service failures to sanitized HTTP errors."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import ServiceError, ServiceUnavailableError
from app.providers.base import ProviderTimeoutError
from app.services.question_generator import InvalidQuestionsError


def register_error_handlers(app: FastAPI) -> None:
    """Register one boundary for errors shared by internal endpoints."""
    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        if isinstance(exc, ProviderTimeoutError):
            code, message = 504, "Gemini request timed out"
        elif isinstance(exc, ServiceUnavailableError):
            code, message = 503, "Generation service unavailable"
        elif isinstance(exc, InvalidQuestionsError):
            code, message = 502, "Gemini returned invalid question JSON"
        else:
            code, message = 502, "Gemini request failed"
        return JSONResponse(status_code=code, content={"detail": message})
