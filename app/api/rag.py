"""Authenticated internal RAG commands; NestJS owns authorization and queueing."""

import asyncio

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import require_internal_token
from app.schemas.rag import (IndexCommand, IndexProfile, IndexProgress, PrepareIndexRequest,
    RetrieveRequest, RetrieveResponse, RagErrorResponse)
from app.services.rag_errors import RagError
from app.services.retrieval import RagService
from app.services.grounded_generation import GroundedGeneration
from app.schemas.grounded_generation import GroundedGenerateRequest, GroundedGenerateResponse
from app.schemas.knowledge_plan import OutlineRequest, OutlineResponse

router = APIRouter(prefix="/internal/rag", tags=["rag"], dependencies=[Depends(require_internal_token)], responses={
    status: {"model": RagErrorResponse, "description": description}
    for status, description in {
        401: "Internal token missing or invalid",
        404: "Authorized source scope unavailable",
        408: "Request body intake timed out",
        409: "Profile, index or lease conflict",
        413: "Actual request body byte limit exceeded",
        422: "Invalid input, source or insufficient context",
        502: "Invalid provider output or provider configuration",
        503: "Capacity, authentication configuration, storage or provider unavailable",
        504: "RAG or generation deadline exceeded",
    }.items()
})


def get_rag(request: Request) -> RagService:
    service = request.app.state.rag
    if service is None:
        raise RagError("RAG_DISABLED", 503)
    return service


def get_grounded(request: Request) -> GroundedGeneration:
    service = request.app.state.grounded_generator
    if service is None:
        raise RagError("RAG_GENERATION_UNAVAILABLE",503)
    return service


@router.post("/generate",response_model=GroundedGenerateResponse)
async def generate(input: GroundedGenerateRequest,service: GroundedGeneration = Depends(get_grounded)) -> GroundedGenerateResponse:
    """Generate evidence-cited questions from fully scoped READY indexes."""
    return await service.generate(input)


@router.post("/outline", response_model=OutlineResponse)
async def outline(input: OutlineRequest, service: RagService = Depends(get_rag)) -> OutlineResponse:
    """Inspect every authorized source unit without a paid planning invocation."""
    from app.services.content_map import content_map
    service.require_profile(input.profile_id)
    try:
        await asyncio.wait_for(service.slots.acquire(), .1)
    except TimeoutError:
        raise RagError("RAG_CAPACITY_REACHED", 503, True) from None
    try:
        async with asyncio.timeout(15):
            chunks = await service.repository.outline_chunks(input.owner_id, input.profile_id, input.index_id)
            result = await asyncio.to_thread(content_map, input.index_id, input.title, chunks)
            await service.repository.validate_scope(input.owner_id, input.profile_id, [input.index_id])
            return result
    except RagError:
        raise
    except TimeoutError:
        raise RagError("RAG_DEADLINE", 504, True) from None
    except Exception:
        raise RagError("RAG_STORAGE_UNAVAILABLE", 503, True) from None
    finally:
        service.slots.release()


@router.get("/profile", response_model=IndexProfile)
async def profile(service: RagService = Depends(get_rag)) -> IndexProfile:
    """Expose the immutable current profile without a paid provider call."""
    return service.profile


@router.post("/prepare", response_model=IndexProgress)
async def prepare(input: PrepareIndexRequest, service: RagService = Depends(get_rag)) -> IndexProgress:
    """Validate source and create a replayable chunk manifest."""
    return await service.run("prepare", input)


@router.post("/step", response_model=IndexProgress)
async def step(input: IndexCommand, service: RagService = Depends(get_rag)) -> IndexProgress:
    """Embed at most one bounded batch and checkpoint its vectors."""
    return await service.run("step", input)


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(input: RetrieveRequest, service: RagService = Depends(get_rag)) -> RetrieveResponse:
    """Retrieve only READY indexes in the authorized owner/profile/id scope."""
    return await service.run("retrieve", input)


@router.post("/revoke", status_code=204)
async def revoke(input: IndexCommand, service: RagService = Depends(get_rag)) -> Response:
    """Revoke technical access, including a tombstone for late prepare requests."""
    await service.run("revoke", input)
    return Response(status_code=204)


class RagBodyLimit:
    """Bound actual JSON bytes and intake time before FastAPI parses a body."""
    def __init__(self, app) -> None:
        self.app = app
        self.slots = asyncio.Semaphore(4)

    async def __call__(self, scope, receive, send) -> None:
        from fastapi.responses import JSONResponse
        if scope["type"] != "http" or not scope.get("path", "").startswith("/internal/rag/"):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            require_internal_token(request, request.headers.get("X-Internal-Token"))
        except RagError as exc:
            await JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "retryable": exc.retryable}})(scope, receive, send)
            return
        try:
            await asyncio.wait_for(self.slots.acquire(), 0.1)
        except TimeoutError:
            await JSONResponse(status_code=503, content={"detail":{"code":"RAG_CAPACITY_REACHED","retryable":True}})(scope, receive, send)
            return
        try:
            payload = await self.bounded(scope, receive, send)
        finally:
            self.slots.release()
        if payload is None:
            return
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type":"http.request", "body":payload, "more_body":False}
        await self.app(scope, replay, send)

    async def bounded(self, scope, receive, send) -> bytes | None:
        from fastapi.responses import JSONResponse
        parts, size = [], 0
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body = message.get("body", b"")
                    size += len(body)
                    if size > 8 * 1024 * 1024:
                        await JSONResponse(status_code=413, content={"detail":{"code":"RAG_INPUT_TOO_LARGE","retryable":False}})(scope, receive, send)
                        return
                    parts.append(body)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse(status_code=408, content={"detail":{"code":"RAG_INPUT_TIMEOUT","retryable":False}})(scope, receive, send)
            return
        return b"".join(parts)
