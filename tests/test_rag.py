"""Deterministic source handling and the installed Google SDK without paid calls."""
import asyncio
import hashlib
import json
import math
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from google import genai

from app.config import Settings
from app.main import create_app
from app.schemas.document import ExtractionResult
from app.schemas.rag import IndexCommand, PrepareIndexRequest, RetrieveRequest, current_profile
from app.services.chunking import chunk_document
from app.services.embedding import GoogleEmbeddingProvider, normalized_vector
from app.services.rag_errors import RagError
from app.services.retrieval import RagService
from app.repositories.windows_pool import SelectorRepository


def source(text="Stack uses LIFO.", kind="PAGE"):
    return ExtractionResult(contract_version="document-extraction.v1",source_type="PDF" if kind=="PAGE" else "PPTX",source_sha256="a"*64,parser_version="fixture/v1",include_notes=kind=="NOTES",status="EXTRACTED",unit_count=1,spans=[{"kind":kind,"position":1,"text":text}],warnings=[])


def command():
    return IndexCommand(index_id=uuid4(),profile_id=current_profile().profile_id,scope={k:uuid4() for k in ("owner_id","topic_id","chapter_id","document_id","version_id")})


@pytest.mark.parametrize("text",["abc "*4000, "\u1ec7\u1ebf\U0001f600 "*2000, "x"*10000, " x " + " "*1000],ids=["words","unicode","unbroken","whitespace"])
def test_chunks_deterministic_offsets_hash_budget_coverage(text):
    index=uuid4(); extracted=source(text)
    chunks=chunk_document(index,extracted)
    assert chunks==chunk_document(index,extracted)
    covered=set()
    for c in chunks:
        assert c.text==text[c.start_offset:c.end_offset]
        assert len(c.text.encode())<=1800
        assert c.text_sha256==hashlib.sha256(c.text.encode()).hexdigest()
        covered.update(range(c.start_offset,c.end_offset))
    assert all(i in covered for i,char in enumerate(text) if not char.isspace())
    assert len({c.chunk_id for c in chunks})==len(chunks)


def test_chunk_units_and_ocr():
    extracted=source("Slide content", "SLIDE")
    extracted.spans.append(type(extracted.spans[0])(kind="NOTES",position=1,text="Speaker notes"))
    extracted.include_notes=True
    assert [c.kind for c in chunk_document(uuid4(),extracted)]==["SLIDE","NOTES"]
    extracted.status="OCR_REQUIRED"
    with pytest.raises(RagError,match="RAG_OCR_REQUIRED"):chunk_document(uuid4(),extracted)


@pytest.mark.parametrize("vector",[[0.]*768,[1.]*767,[float("nan")]*768,[True]*768])
def test_reject_invalid_vectors(vector):
    with pytest.raises(RagError,match="RAG_EMBEDDING_INVALID"):normalized_vector(vector)


def install(monkeypatch,handler):
    original=genai.Client
    def factory(**kwargs):
        kwargs["http_options"].async_client_args={"transport":httpx.MockTransport(handler)}
        return original(**kwargs)
    monkeypatch.setattr("app.services.embedding.genai.Client",factory)


def test_real_sdk_tasks_dimensions_normalization_and_client_reuse(monkeypatch):
    calls=[]
    def handler(req):
        calls.append((req.url.path,json.loads(req.content)))
        count=len(calls[-1][1]["requests"])
        return httpx.Response(200,json={"embeddings":[{"values":[2.]*768} for _ in range(count)]})
    install(monkeypatch,handler)
    async def run():
        provider=GoogleEmbeddingProvider("fixture-key")
        try:
            for task in ("RETRIEVAL_DOCUMENT","RETRIEVAL_QUERY"):
                vectors=await provider.embed(["LIFO","FIFO"],task)
                assert len(vectors)==2 and math.isclose(math.hypot(*vectors[0]),1.)
        finally:await provider.aclose()
    asyncio.run(run())
    assert len(calls)==2
    assert calls[0][0].endswith("/models/gemini-embedding-001:batchEmbedContents")
    assert calls[0][1]["requests"][0]["taskType"]=="RETRIEVAL_DOCUMENT"
    assert calls[1][1]["requests"][0]["taskType"]=="RETRIEVAL_QUERY"
    assert calls[0][1]["requests"][0]["outputDimensionality"]==768


@pytest.mark.parametrize("status,retryable",[(403,False),(429,True),(503,True)])
def test_embedding_failure_no_nested_retries(monkeypatch,caplog,status,retryable):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(status,json={"error":{"code":status,"message":"PRIVATE-SOURCE"}})
    install(monkeypatch,handler)
    async def run():
        provider=GoogleEmbeddingProvider("fixture-key")
        try:
            with pytest.raises(RagError) as error:await provider.embed(["source"],"RETRIEVAL_DOCUMENT")
            assert error.value.retryable is retryable
        finally:await provider.aclose()
    asyncio.run(run());assert len(calls)==1
    assert "PRIVATE-SOURCE" not in caplog.text


def test_timeout_and_cancellation_release_slots(monkeypatch):
    def handler(req):raise httpx.ReadTimeout("PRIVATE",request=req)
    install(monkeypatch,handler)
    async def run():
        provider=GoogleEmbeddingProvider("fixture-key")
        try:
            with pytest.raises(RagError) as error:await provider.embed(["source"],"RETRIEVAL_QUERY")
            assert error.value.status==504
        finally:await provider.aclose()
        repo=AsyncMock();embedding=AsyncMock()
        embedding.embed.side_effect=lambda *args: asyncio.sleep(99)
        # Explicit cancellation during a repository wait releases the service slot.
        async def hold(*args):await asyncio.sleep(99)
        repo.validate_scope.side_effect=hold
        service=RagService(repo,embedding)
        req=RetrieveRequest(owner_id=uuid4(),profile_id=current_profile().profile_id,index_ids=[uuid4()],query="source")
        task=asyncio.create_task(service.run("retrieve",req));await asyncio.sleep(.01);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert service.slots._value==2
    asyncio.run(run())


def test_retrieval_scope_rechecked_and_context_bounded():
    async def run():
        repo=AsyncMock();embedding=AsyncMock();embedding.embed.return_value=[[1.]+[0.]*767]
        req=RetrieveRequest(owner_id=uuid4(),profile_id=current_profile().profile_id,index_ids=[uuid4()],query="LIFO",top_k=8)
        rows=[]
        for i in range(20):
            chunk=chunk_document(uuid4(),source("x"*1750))[0]
            rows.append({**chunk.model_dump(),"index_id":req.index_ids[0],"document_id":uuid4(),"version_id":req.index_ids[0],"source_sha256":"a"*64,"position":i//3+1,"similarity":.9})
        repo.search.return_value=rows
        service=RagService(repo,embedding)
        result=await service.run("retrieve",req)
        assert result.context_bytes<=12000 and len(result.chunks)<=8
        assert repo.validate_scope.await_count==2
        repo.validate_scope.side_effect=RagError("RAG_SCOPE_UNAVAILABLE",404)
        embedding.reset_mock()
        with pytest.raises(RagError):await service.run("retrieve",req)
        embedding.embed.assert_not_awaited()
    asyncio.run(run())


def test_embedding_checkpoint_saved_and_lease_released():
    async def run():
        repo=AsyncMock();embedding=AsyncMock();req=command()
        row={"id":req.index_id,"profile_id":req.profile_id,"status":"BUILDING","chunk_count":1,"completed_chunks":0}
        repo.claim.return_value=(row,[{"id":uuid4(),"text":"source"}])
        repo.save.return_value={**row,"status":"READY","completed_chunks":1}
        embedding.embed.return_value=[[1.]+[0.]*767]
        service=RagService(repo,embedding)
        assert (await service.run("step",req)).status=="READY"
        repo.save.assert_awaited_once();repo.release.assert_awaited_once()
        repo.reset_mock();embedding.embed.side_effect=RagError("RAG_EMBEDDING_TIMEOUT",504,True)
        with pytest.raises(RagError):await service.run("step",req)
        repo.save.assert_not_awaited();repo.release.assert_awaited_once()
    asyncio.run(run())


def test_http_auth_disabled_safe_validation_and_actual_body_limit():
    with TestClient(create_app(Settings(internal_api_token="fixture-token"))) as client:
        assert client.get("/internal/rag/profile").status_code==401
        headers={"X-Internal-Token":"fixture-token"}
        assert client.get("/internal/rag/profile",headers=headers).status_code==503
        # Install only a fake service to exercise production request validation.
        client.app.state.rag=RagService(AsyncMock(),AsyncMock())
        response=client.post("/internal/rag/retrieve",headers=headers,json={"query":"PRIVATE-SOURCE"})
        assert response.status_code==422 and "PRIVATE-SOURCE" not in response.text
        response=client.post("/internal/rag/prepare",headers=headers,content=b"x"*(8*1024*1024+1))
        assert response.status_code==413
        assert client.get("/ready").json()["rag"]=="disabled"


def test_selector_bridge_owns_db_loop_and_closes():
    import threading
    async def run():
        origin=threading.get_ident()
        class Repo:
            async def open(self):self.thread=threading.get_ident()
            async def identity(self):return threading.get_ident()
            async def close(self):pass
        proxy=SelectorRepository();await proxy.open(Repo)
        assert await proxy.identity()!=origin
        await proxy.close();assert not proxy.thread.is_alive()
    asyncio.run(run())


def test_rag_openapi_contract_does_not_drift():
    from scripts.export_document_contract import document_contract, ROOT
    assert json.loads((ROOT/"contracts/rag-indexing.openapi.json").read_text(encoding="utf-8"))==document_contract("/internal/rag/")
