"""Scope failures must never write tombstones or change existing index state."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.repositories.vector_repository import VectorRepository
from app.services.rag_errors import RagError
from tests.test_rag import command


def repository(existing):
    cursor = SimpleNamespace(fetchone=AsyncMock(return_value=existing))
    db = SimpleNamespace(execute=AsyncMock(return_value=cursor))
    @asynccontextmanager
    async def connection():
        yield db
    repo = object.__new__(VectorRepository)
    repo.pool = SimpleNamespace(connection=connection)
    repo._scoped = AsyncMock(side_effect=AssertionError("Revocation must allow replay"))
    return repo, db


@pytest.mark.parametrize("field", ["profile_id", "owner_id", "topic_id", "chapter_id", "document_id", "version_id"])
def test_revoke_rejects_each_scope_mismatch_without_writes(field):
    async def run():
        req = command()
        row = {**req.scope.model_dump(), "profile_id": req.profile_id}
        row[field] = "b" * 64 if field == "profile_id" else uuid4()
        repo, db = repository(row)
        with pytest.raises(RagError) as error:
            await repo.revoke(req)
        assert error.value.code == "RAG_SCOPE_UNAVAILABLE"
        assert error.value.status == 404
        statements = [call.args[0].lstrip() for call in db.execute.await_args_list]
        assert not any(sql.startswith(("INSERT", "UPDATE")) for sql in statements)
        repo._scoped.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("state", [None, "READY", "REVOKED"])
def test_revoke_records_tombstone_for_absent_or_matching_scope_and_allows_replay(state):
    async def run():
        req = command()
        row = None if state is None else {**req.scope.model_dump(), "profile_id": req.profile_id, "status": state}
        repo, db = repository(row)
        await repo.revoke(req)
        statements = [call.args[0].lstrip() for call in db.execute.await_args_list]
        assert any(sql.startswith("INSERT INTO rag.revoked_indexes") for sql in statements)
        assert any(sql.startswith("UPDATE rag.document_indexes") for sql in statements)
        repo._scoped.assert_not_awaited()
    asyncio.run(run())
