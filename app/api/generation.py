"""Authenticated internal question generation endpoint."""

from fastapi import APIRouter, Depends

from app.api.dependencies import get_generator, require_internal_token
from app.schemas.question import GenerateRequest, GenerateResponse
from app.services.question_generator import QuestionGenerator

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post(
    "/generate",
    response_model=GenerateResponse,
    dependencies=[Depends(require_internal_token)],
    summary="Generate single-choice question drafts from supplied text",
)
async def generate_questions(
    payload: GenerateRequest,
    generator: QuestionGenerator = Depends(get_generator),
) -> GenerateResponse:
    """Return validated drafts without writing them to the question bank."""
    return await generator.generate(payload)
