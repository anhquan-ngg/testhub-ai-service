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
from app.services.chunking import chunk_document
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
            await repo.open()
            assert not await repo.ready()
            # Reproduce old-schema failure before applying the additive migration.
            chapterless=command();chapterless.scope.chapter_id=None
            request=PrepareIndexRequest(**chapterless.model_dump(),extraction=source())
            with pytest.raises(psycopg.errors.NotNullViolation) as error:
                await repo.prepare(request,current_profile(),chunk_document(request.index_id,request.extraction),"b"*64)
            assert error.value.diag.column_name=="chapter_id"
            await setup.execute((Path(__file__).resolve().parents[1]/"migrations/002_optional_document_chapter.sql").read_text());await setup.commit()
            assert await repo.ready()
            # A migration ledger row alone must not hide an incompatible column.
            await setup.execute("ALTER TABLE rag.document_indexes ALTER COLUMN chapter_id SET NOT NULL");await setup.commit()
            assert not await repo.ready()
            await setup.execute("ALTER TABLE rag.document_indexes ALTER COLUMN chapter_id DROP NOT NULL");await setup.commit()
            assert await repo.ready()
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
            # A topic-owned source has no bank chapter. Filter BEFORE top-40,
            # otherwise a late page with identical embeddings is unreachable.
            planned=command();planned.scope.chapter_id=None;planned.scope.owner_id=req.scope.owner_id
            extracted=source("Topic evidence")
            extracted.spans=[type(extracted.spans[0])(kind="PAGE",position=i,text=f"Evidence page {i}.") for i in range(1,51)]
            extracted.unit_count=50
            await service.run("prepare",PrepareIndexRequest(**planned.model_dump(),extraction=extracted))
            while (await service.run("step",planned)).status!="READY":pass
            scoped=await repo.search(planned.scope.owner_id,planned.profile_id,[planned.index_id],[1.]+[0.]*767,source_units=[{"index_id":str(planned.index_id),"kind":"PAGE","position":50}])
            assert scoped and all(row["position"]==50 for row in scoped)
            manifest=await repo.outline_chunks(planned.scope.owner_id,planned.profile_id,planned.index_id)
            assert {row["position"] for row in manifest}==set(range(1,51))
            assert await repo.search(uuid4(),planned.profile_id,[planned.index_id],[1.]+[0.]*767,source_units=[{"index_id":str(planned.index_id),"kind":"PAGE","position":50}])==[]
            scoped=await repo.search(req.scope.owner_id,req.profile_id,[req.index_id,planned.index_id],[1.]+[0.]*767,
                source_units=[{"index_id":str(planned.index_id),"kind":"PAGE","position":1}])
            assert scoped and all(row["index_id"]==planned.index_id and row["position"]==1 for row in scoped)
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
