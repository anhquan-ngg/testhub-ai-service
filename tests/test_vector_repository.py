"""Opt-in PostgreSQL/pgvector integration, never uses RAG_DATABASE_URL or .env."""
import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from app.repositories.vector_repository import VectorRepository
from app.schemas.rag import PrepareIndexRequest, current_profile
from app.services.retrieval import RagService
from app.services.rag_errors import RagError
from tests.test_rag import command, source
from tests.rag_http_fixture import Embedding

@pytest.mark.skipif(not os.getenv("RAG_TEST_DATABASE_URL"),reason="Requires a disposable loopback *_test database with vector installed")
def test_real_pgvector_checkpoint_scope_lease_and_tombstone():
    dsn=os.environ["RAG_TEST_DATABASE_URL"]
    url=urlparse(dsn)
    if url.hostname not in ("127.0.0.1","localhost") or not url.path.endswith("_test"):
        pytest.fail("Use a disposable loopback *_test database")
    async def run():
        # Deliberately fail if rag already exists; never delete pre-existing data.
        setup=await psycopg.AsyncConnection.connect(dsn)
        created=False;repo=VectorRepository(dsn)
        try:
            await setup.execute("CREATE SCHEMA rag");await setup.commit();created=True
            sql=(Path(__file__).resolve().parents[1]/"migrations/001_rag.sql").read_text()
            await setup.execute(sql);await setup.commit()
            await repo.open();assert await repo.ready()
            req=command();prep=PrepareIndexRequest(**req.model_dump(),extraction=source("LIFO. "*1000))
            service=RagService(repo,Embedding())
            result=await service.run("prepare",prep)
            replay=await service.run("prepare",prep);assert replay==result
            token=uuid4();await repo.claim(req,token)
            with pytest.raises(RagError,match="RAG_INDEX_BUSY"):await repo.claim(req,uuid4())
            await repo.release(req.index_id,token)
            while (await service.run("step",req)).status!="READY":pass
            assert (await repo.claim(req,uuid4()))[1]==[]
            await repo.validate_scope(req.scope.owner_id,req.profile_id,[req.index_id])
            rows=await repo.search(req.scope.owner_id,req.profile_id,[req.index_id],[1.]+[0.]*767)
            assert rows and rows[0]["source_sha256"]==prep.extraction.source_sha256
            assert await repo.search(uuid4(),req.profile_id,[req.index_id],[1.]+[0.]*767)==[]
            with pytest.raises(RagError):await repo.save(req,uuid4(),[])
            await repo.revoke(req)
            with pytest.raises(RagError):await repo.validate_scope(req.scope.owner_id,req.profile_id,[req.index_id])
            with pytest.raises(RagError):await service.run("prepare",prep)
            late=command();await repo.revoke(late)
            with pytest.raises(RagError):await service.run("prepare",PrepareIndexRequest(**late.model_dump(),extraction=source()))
        finally:
            await repo.close()
            if created:
                await setup.rollback();await setup.execute("DROP SCHEMA rag CASCADE");await setup.commit()
            await setup.close()
    # psycopg needs selector on Windows; extraction uses Proactor in normal service.
    import sys
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform=="win32" else asyncio.new_event_loop) as runner:
        runner.run(run())
