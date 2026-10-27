"""Bounded RAG indexing/retrieval orchestration; no question-bank writes."""

import asyncio
import hashlib
import json
import logging
from typing import Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from app.schemas.grounded_generation import GroundedGenerateRequest
    from app.repositories.vector_repository import VectorRepository
    from app.repositories.windows_pool import SelectorRepository
from uuid import uuid4

from app.schemas.rag import (IndexCommand, IndexProgress, MAX_CONTEXT_BYTES, PrepareIndexRequest,
    RetrievedChunk, RetrieveRequest, RetrieveResponse, current_profile)
from app.services.chunking import chunk_document
from app.services.embedding import EmbeddingProvider
from app.services.rag_errors import RagError

logger = logging.getLogger(__name__)


class RagService:
    def __init__(self, repository: "VectorRepository | SelectorRepository", embedding: EmbeddingProvider) -> None:
        self.repository, self.embedding = repository, embedding
        self.profile = current_profile()
        self.slots = asyncio.Semaphore(2)

    async def run(self, operation: Literal["prepare", "step", "retrieve", "revoke", "verify_scope"], request: IndexCommand | PrepareIndexRequest | RetrieveRequest) -> IndexProgress | RetrieveResponse | None:
        """Bound total work/admission and sanitize storage/SDK errors centrally."""
        try:
            await asyncio.wait_for(self.slots.acquire(), 0.1)
        except TimeoutError:
            raise RagError("RAG_CAPACITY_REACHED", 503, True) from None
        try:
            return await asyncio.wait_for(getattr(self, operation)(request), 40)
        except RagError:
            raise
        except TimeoutError:
            raise RagError("RAG_DEADLINE", 504, True) from None
        except Exception as exc:
            logger.warning("rag_storage_failed exception_type=%s", type(exc).__name__)
            raise RagError("RAG_STORAGE_UNAVAILABLE", 503, True) from None
        finally:
            self.slots.release()

    def require_profile(self, profile: str) -> None:
        if profile != self.profile.profile_id:
            raise RagError("RAG_PROFILE_MISMATCH", 409)

    @staticmethod
    def progress(row: dict) -> IndexProgress:
        return IndexProgress(index_id=row["id"], profile_id=row["profile_id"], status=row["status"],
            chunk_count=row["chunk_count"], completed_chunks=row["completed_chunks"])

    async def prepare(self, request: PrepareIndexRequest) -> IndexProgress:
        self.require_profile(request.profile_id)
        chunks = chunk_document(request.index_id, request.extraction)
        fingerprint = hashlib.sha256(json.dumps(request.model_dump(mode="json"), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return self.progress(await self.repository.prepare(request, self.profile, chunks, fingerprint))

    async def step(self, request: IndexCommand) -> IndexProgress:
        self.require_profile(request.profile_id)
        token = uuid4()
        row, chunks = await self.repository.claim(request, token)
        if row["status"] == "READY":
            return self.progress(row)
        try:
            vectors = await self.embedding.embed([c["text"] for c in chunks], "RETRIEVAL_DOCUMENT") if chunks else []
            return self.progress(await self.repository.save(request, token, [(c["id"], v) for c,v in zip(chunks,vectors, strict=True)]))
        finally:
            try:
                await asyncio.wait_for(self.repository.release(request.index_id, token), 3)
            except Exception:
                logger.warning("rag_lease_release_deferred")

    async def retrieve(self, request: RetrieveRequest) -> RetrieveResponse:
        self.require_profile(request.profile_id)
        await self.repository.validate_scope(request.owner_id, request.profile_id, request.index_ids)
        vector = (await self.embedding.embed([request.query], "RETRIEVAL_QUERY"))[0]
        candidates = await self.repository.search(request.owner_id, request.profile_id, request.index_ids, vector,
            **({"source_units": [unit.model_dump(mode="json") for unit in request.source_units]} if request.source_units else {}))
        selected, used, units = [], 0, {}
        for candidate in candidates:
            chunk = RetrievedChunk.model_validate(candidate)
            if request.source_units and not any(unit.index_id == chunk.index_id and unit.kind == chunk.kind and unit.position == chunk.position for unit in request.source_units):
                continue
            unit = (chunk.version_id, chunk.kind, chunk.position)
            size = len(chunk.text.encode())
            if units.get(unit, 0) >= 2 or used + size > MAX_CONTEXT_BYTES:
                continue
            units[unit] = units.get(unit, 0) + 1
            used += size
            selected.append(chunk)
            if len(selected) == request.top_k:
                break
        # Recheck the whole scope after provider/DB I/O, not just returned rows.
        await self.repository.validate_scope(request.owner_id, request.profile_id, request.index_ids)
        if not selected:
            raise RagError("RAG_INSUFFICIENT_CONTEXT")
        return RetrieveResponse(profile_id=request.profile_id, context_bytes=used, chunks=selected)

    async def revoke(self, request: IndexCommand) -> None:
        await self.repository.revoke(request)

    async def retrieve_plan(self, request: "GroundedGenerateRequest") -> dict[str, list[RetrievedChunk]]:
        """Embed bounded per-objective queries in batches; never replace missing plan evidence."""
        from app.schemas.rag import BATCH_SIZE
        self.require_profile(request.profile_id)
        if not request.plan_items:
            raise RagError("RAG_PLAN_INVALID", 422)
        try:
            await asyncio.wait_for(self.slots.acquire(), .1)
        except TimeoutError:
            raise RagError("RAG_CAPACITY_REACHED", 503, True) from None
        try:
            await self.repository.validate_scope(request.owner_id, request.profile_id, request.index_ids)
            queries = []
            for item in request.plan_items:
                query = f"{item.knowledge_title}: {item.objective} ({item.question_format.value})"
                while len(query.encode()) > 1800:
                    query = query[:-1]
                queries.append(query)
            vectors = []
            for offset in range(0, len(queries), BATCH_SIZE):
                vectors.extend(await self.embedding.embed(queries[offset:offset + BATCH_SIZE], "RETRIEVAL_QUERY"))
            contexts, used = {}, 0
            for item, vector in zip(request.plan_items, vectors, strict=True):
                candidates = await self.repository.search(request.owner_id, request.profile_id, request.index_ids, vector,
                    source_units=[{"index_id": str(request.index_ids[0]), **unit.model_dump()} for unit in item.source_units])
                allowed = {(request.index_ids[0], unit.kind, unit.position) for unit in item.source_units}
                chosen = []
                for candidate in candidates:
                    chunk = RetrievedChunk.model_validate(candidate)
                    if (chunk.index_id, chunk.kind, chunk.position) not in allowed or used + len(chunk.text.encode()) > 72_000:
                        continue
                    chosen.append(chunk)
                    used += len(chunk.text.encode())
                    if len(chosen) == 2:
                        break
                if not chosen:
                    raise RagError("RAG_PLAN_ITEM_INSUFFICIENT_CONTEXT", 422)
                contexts[item.id] = chosen
            await self.repository.validate_scope(request.owner_id, request.profile_id, request.index_ids)
            return contexts
        except RagError:
            raise
        except TimeoutError:
            raise RagError("RAG_DEADLINE", 504, True) from None
        except Exception:
            raise RagError("RAG_STORAGE_UNAVAILABLE", 503, True) from None
        finally:
            self.slots.release()

    async def verify_scope(self, request: RetrieveRequest) -> None:
        """Recheck authorization without another embedding request."""
        self.require_profile(request.profile_id)
        await self.repository.validate_scope(request.owner_id,request.profile_id,request.index_ids)
