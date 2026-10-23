"""Central mapping from expected service failures to sanitized HTTP errors."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse

from app.errors import ServiceError, ServiceUnavailableError
from app.providers.base import ProviderTimeoutError
from app.services.question_generator import InvalidQuestionsError
from app.services.document_parser import ExtractionError
from app.services.rag_errors import RagError


def register_error_handlers(app: FastAPI) -> None:
    """Register one boundary for errors shared by internal endpoints."""
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith("/internal/rag/"):
            return JSONResponse(status_code=422, content={"detail":{"code":"RAG_INPUT_INVALID","retryable":False}})
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(RagError)
    async def rag_error_handler(request: Request, exc: RagError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "retryable": exc.retryable}})

    @app.exception_handler(ExtractionError)
    async def extraction_error_handler(request: Request, exc: ExtractionError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.code})

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
        if request.url.path.startswith("/internal/rag/"):
            rag_code = "RAG_OUTPUT_INVALID" if isinstance(exc,InvalidQuestionsError) else "RAG_GENERATION_TIMEOUT" if isinstance(exc,ProviderTimeoutError) else "RAG_GENERATION_UNAVAILABLE" if isinstance(exc,ServiceUnavailableError) else "RAG_GENERATION_FAILED"
            return JSONResponse(status_code=code,content={"detail":{"code":rag_code,"retryable":False}})
        return JSONResponse(status_code=code, content={"detail": message})
