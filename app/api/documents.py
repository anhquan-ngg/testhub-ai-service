"""Internal binary extraction API. Never accepts arbitrary download URLs."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.dependencies import require_internal_token
from app.schemas.document import ExtractionResult
from app.services.document_parser import DocumentParser

router = APIRouter(prefix="/internal/documents", tags=["documents"],
                   dependencies=[Depends(require_internal_token)])


@router.post("/extract", response_model=ExtractionResult, responses={
    401: {"description": "Internal token missing or invalid"},
    413: {"description": "Actual input byte limit exceeded"},
    415: {"description": "application/octet-stream required"},
    422: {"description": "Invalid, encrypted, unsupported or excessive document"},
    503: {"description": "Capacity reached or processing infrastructure unavailable"},
    504: {"description": "Overall extraction deadline exceeded"},
}, openapi_extra={
    "requestBody": {"required": True, "content": {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
    }},
})
async def extract_document(
    request: Request, source_type: Literal["PDF", "PPTX"], include_notes: bool = False,
) -> ExtractionResult:
    """Receive owner-authorized bytes from NestJS and return source-positioned text."""
    if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
        raise HTTPException(415, "application/octet-stream required")
    if source_type == "PDF" and include_notes:
        raise HTTPException(422, "Speaker notes are only supported for PPTX")
    parser: DocumentParser = request.app.state.document_parser
    return await parser.extract(request.stream(), source_type, include_notes)
