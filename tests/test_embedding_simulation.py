"""Opt-in real SDK/HTTP transport test against the committed local Postman mock."""
import asyncio
import os
import pytest
from google import genai
from app.services.embedding import GoogleEmbeddingProvider
from app.services.rag_errors import RagError

@pytest.mark.skipif(os.getenv("RUN_EMBEDDING_SIMULATION")!="1",reason="Start local Postman simulation on 4962")
def test_sdk_real_http_embedding_and_failures(monkeypatch):
    original=genai.Client
    def factory(**kwargs):
        kwargs["http_options"].base_url="http://127.0.0.1:4962"
        return original(**kwargs)
    monkeypatch.setattr("app.services.embedding.genai.Client",factory)
    async def run():
        provider=GoogleEmbeddingProvider("synthetic-fixture-key")
        try:
            result=await provider.embed(["Stack uses LIFO."],"RETRIEVAL_DOCUMENT")
            assert len(result[0])==768 and result[0][0]==1.
            for query,retry in [("fixture-quota",True),("fixture-config",False)]:
                with pytest.raises(RagError) as error:await provider.embed([query],"RETRIEVAL_QUERY")
                assert error.value.retryable is retry
        finally:await provider.aclose()
    asyncio.run(run())


@pytest.mark.skipif(os.getenv("RUN_EMBEDDING_SIMULATION")!="1",reason="Start local Postman simulation on 4962")
def test_retrieval_and_grounded_generation_with_real_sdk_http(monkeypatch):
    from app.config import Settings
    from app.providers.gemini import GeminiProvider
    from app.services.question_generator import QuestionGenerator
    from app.services.grounded_generation import GroundedGeneration
    from app.services.retrieval import RagService
    from app.schemas.rag import PrepareIndexRequest
    from app.schemas.grounded_generation import GroundedGenerateRequest
    from tests.rag_http_fixture import Repository
    from tests.test_rag import command,source
    original=genai.Client
    def factory(**kwargs):
        kwargs["http_options"].base_url="http://127.0.0.1:4962"
        return original(**kwargs)
    monkeypatch.setattr("app.services.embedding.genai.Client",factory)
    async def run():
        embedding=GoogleEmbeddingProvider("synthetic-fixture-key")
        provider=GeminiProvider(Settings(gemini_api_key="synthetic-fixture-key",gemini_model="fixture-model",gemini_max_attempts=1))
        try:
            rag=RagService(Repository(),embedding);req=command()
            await rag.run("prepare",PrepareIndexRequest(**req.model_dump(),extraction=source("A stack uses the Last In First Out (LIFO) principle.")))
            await rag.run("step",req)
            service=GroundedGeneration(rag,QuestionGenerator(provider))
            result=await service.generate(GroundedGenerateRequest(owner_id=req.scope.owner_id,profile_id=req.profile_id,index_ids=[req.index_id],query="stack ordering",count=1,question_format="KNOWLEDGE"))
            assert result.questions[0].citations[0].chunk.version_id==req.scope.version_id
            assert result.quality_status=="UNASSESSED"
        finally:
            await embedding.aclose();await provider.aclose()
    asyncio.run(run())
