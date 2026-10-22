"""RAG generation checks against exact source evidence, without external services."""
import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.schemas.grounded_generation import GroundedGenerateRequest
from app.schemas.rag import PrepareIndexRequest
from app.services.grounded_generation import GroundedGeneration
from app.services.question_generator import QuestionGenerator
from app.providers.base import ProviderTimeoutError
from tests.rag_http_fixture import Repository, Embedding
from app.services.retrieval import RagService
from tests.test_rag import command, source

TOKEN="grounded-fixture-token"
SOURCE="A stack uses the Last In First Out (LIFO) principle."

class Provider:
    def __init__(self):self.calls=0;self.change=lambda q:q;self.callback=None;self.slow=False
    async def generate_grounded(self,prompt):
        self.calls+=1
        if self.slow:await asyncio.sleep(99)
        if self.callback:await self.callback()
        data=json.loads(prompt.split("Reference JSON follows:\n",1)[1]);chunk=data["excerpts"][0]
        question={"question_text":"Which ordering principle does a stack use?","options":[{"text":text,"isCorrect":i==0} for i,text in enumerate(["LIFO","FIFO","Random","Sorted"])],"citations":[{"chunk_id":chunk["chunk_id"],"quote":chunk["text"]}]}
        return json.dumps(self.change({"questions":[question]}))
    async def generate(self,prompt):raise AssertionError("No text fallback")
    async def aclose(self):pass

@pytest.fixture
def context():
    provider=Provider();repo=Repository();rag=RagService(repo,Embedding());generator=QuestionGenerator(provider)
    with TestClient(create_app(Settings(internal_api_token=TOKEN))) as client:
        client.app.state.rag=rag
        client.app.state.grounded_generator=GroundedGeneration(rag,generator)
        req=command()
        prepare=PrepareIndexRequest(**req.model_dump(),extraction=source(SOURCE))
        headers={"X-Internal-Token":TOKEN}
        assert client.post("/internal/rag/prepare",headers=headers,json=prepare.model_dump(mode="json")).status_code==200
        assert client.post("/internal/rag/step",headers=headers,json=req.model_dump(mode="json")).status_code==200
        body={"owner_id":str(req.scope.owner_id),"profile_id":req.profile_id,"index_ids":[str(req.index_id)],"query":"Stack ordering","top_k":8,"count":1,"question_format":"KNOWLEDGE"}
        yield client,provider,repo,req,body,headers,generator


def test_grounded_http_exact_evidence_and_strict_contract(context):
    client,provider,_,req,body,headers,_=context
    response=client.post("/internal/rag/generate",headers=headers,json=body)
    assert response.status_code==200,response.text
    result=response.json();assert result["contract_version"]=="rag-generation.v1" and result["quality_status"]=="UNASSESSED"
    citation=result["questions"][0]["citations"][0]
    assert citation["quote"]==SOURCE and citation["chunk"]["version_id"]==str(req.scope.version_id)
    assert provider.calls==1
    assert client.post("/internal/rag/generate",headers=headers,json={**body,"count":"1"}).status_code==422
    assert client.post("/internal/rag/generate",json=body).status_code==401

@pytest.mark.parametrize("mode",["quote","chunk","duplicates","count","invalid-answer"])
def test_reject_unsupported_evidence_without_retry(context,mode):
    client,provider,_,_,body,headers,_=context
    def change(value):
        question=value["questions"][0]
        if mode=="quote":question["citations"][0]["quote"]="PRIVATE fabricated evidence"
        if mode=="chunk":question["citations"][0]["chunk_id"]=str(uuid4())
        if mode=="duplicates":question["citations"]*=2
        if mode=="count":value["questions"]=[]
        if mode=="invalid-answer":question["options"][1]["isCorrect"]=True
        return value
    provider.change=change
    response=client.post("/internal/rag/generate",headers=headers,json=body)
    assert response.status_code==502 and "PRIVATE" not in response.text
    assert response.json()["detail"]["code"]=="RAG_OUTPUT_INVALID"
    assert provider.calls==1


def test_scope_before_and_after_llm(context):
    client,provider,repo,req,body,headers,_=context
    response=client.post("/internal/rag/generate",headers=headers,json={**body,"owner_id":str(uuid4())})
    assert response.status_code==404 and provider.calls==0
    async def revoke():await repo.revoke(req)
    provider.callback=revoke
    response=client.post("/internal/rag/generate",headers=headers,json=body)
    assert response.status_code==404 and provider.calls==1


def test_cancellation_releases_shared_generation_slot(context):
    _,provider,_,_,body,_,_=context
    async def run():
        provider.slow=True
        # Populate context via the same source manifest used by the HTTP fixture.
        from app.services.chunking import chunk_document
        from app.schemas.rag import RetrieveResponse,RetrievedChunk
        req=GroundedGenerateRequest(**body)
        chunk=chunk_document(uuid4(),source(SOURCE))[0]
        evidence=RetrievedChunk(**chunk.model_dump(),index_id=req.index_ids[0],document_id=uuid4(),version_id=uuid4(),source_sha256="a"*64,similarity=1.)
        response=RetrieveResponse(profile_id=req.profile_id,context_bytes=len(SOURCE.encode()),chunks=[evidence])
        generator=QuestionGenerator(provider,timeout_ms=20,max_concurrency=1)
        with pytest.raises(ProviderTimeoutError):await generator.generate_grounded(req,response)
        assert generator._semaphore._value==1
    asyncio.run(run())


def test_post_llm_scope_check_bypasses_rag_admission(context):
    client, provider, _, _, body, headers, _ = context
    rag = client.app.state.rag
    async def occupy_slots():
        await rag.slots.acquire()
        await rag.slots.acquire()
    provider.callback = occupy_slots
    try:
        response = client.post("/internal/rag/generate", headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert provider.calls == 1
    finally:
        rag.slots.release()
        rag.slots.release()


@pytest.mark.parametrize("mode,status,code", [
    ("storage", 503, "RAG_STORAGE_UNAVAILABLE"),
    ("timeout", 504, "RAG_DEADLINE"),
])
def test_post_llm_scope_check_sanitizes_failure_and_has_own_deadline(context, monkeypatch, mode, status, code):
    client, provider, repo, _, body, headers, _ = context
    async def broken_scope(*args):
        if mode == "timeout":
            await asyncio.sleep(10)
        raise RuntimeError("PRIVATE storage details")
    async def after_llm():
        repo.validate_scope = broken_scope
    provider.callback = after_llm
    original = asyncio.wait_for
    async def bounded(awaitable, timeout):
        return await original(awaitable, .01 if timeout == 5 else timeout)
    monkeypatch.setattr(asyncio, "wait_for", bounded)
    response = client.post("/internal/rag/generate", headers=headers, json=body)
    assert response.status_code == status
    assert response.json() == {"detail": {"code": code, "retryable": True}}
    assert "PRIVATE" not in response.text
    assert provider.calls == 1
