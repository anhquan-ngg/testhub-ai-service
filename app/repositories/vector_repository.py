"""AI-owned PostgreSQL access. All values are bound; no access to public business tables."""

import json
import logging
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.schemas.rag import BATCH_SIZE, Chunk, IndexCommand, IndexProfile, PrepareIndexRequest
from app.services.rag_errors import RagError


class SafePoolLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        """Do not let connection errors include DSNs, usernames or SDK messages."""
        record.msg, record.args, record.exc_info = "rag_pool_connection_unavailable", (), None
        return True


class VectorRepository:
    def __init__(self, dsn: str) -> None:
        logging.getLogger("psycopg.pool").addFilter(SafePoolLog())
        self.pool = AsyncConnectionPool(dsn, min_size=0, max_size=4, max_waiting=8,
            timeout=3, open=False, reconnect_timeout=5,
            kwargs={"row_factory": dict_row, "connect_timeout": 3,
                    "options": "-c statement_timeout=5000 -c lock_timeout=2000 -c search_path=rag,public"})

    async def open(self) -> None:
        """Open the pool without running migrations or requiring Gemini connectivity."""
        await self.pool.open(wait=False)

    async def close(self) -> None:
        await self.pool.close(timeout=5)

    async def ready(self) -> bool:
        async with self.pool.connection() as db:
            row = await (await db.execute("SELECT version FROM rag.schema_migrations WHERE version=1")).fetchone()
            return row is not None

    @staticmethod
    def scope_values(command: IndexCommand) -> tuple:
        s = command.scope
        return (command.index_id, command.profile_id, s.owner_id, s.topic_id, s.chapter_id, s.document_id, s.version_id)

    async def _scoped(self, db, command: IndexCommand, *, lock=False) -> dict:
        sql = """SELECT * FROM rag.document_indexes WHERE id=%s AND profile_id=%s
          AND owner_id=%s AND topic_id=%s AND chapter_id=%s AND document_id=%s AND version_id=%s"""
        if lock:
            sql += " FOR UPDATE"
        row = await (await db.execute(sql, self.scope_values(command))).fetchone()
        if row is None or row["status"] == "REVOKED":
            raise RagError("RAG_SCOPE_UNAVAILABLE", 404)
        return row

    async def prepare(self, request: PrepareIndexRequest, profile: IndexProfile,
                      chunks: list[Chunk], fingerprint: str) -> dict:
        """Create plan/chunks once, or replay the exact pinned request."""
        async with self.pool.connection() as db:
            await db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(request.index_id),))
            revoked = await (await db.execute("SELECT 1 FROM rag.revoked_indexes WHERE index_id=%s", (request.index_id,))).fetchone()
            if revoked:
                raise RagError("RAG_SCOPE_UNAVAILABLE", 404)
            old = await (await db.execute("SELECT request_hash FROM rag.document_indexes WHERE id=%s", (request.index_id,))).fetchone()
            if old:
                if old["request_hash"] != fingerprint:
                    raise RagError("RAG_INDEX_CONFLICT", 409)
                return await self._scoped(db, request)
            await db.execute("INSERT INTO rag.index_profiles(id,config) VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING",
                             (profile.profile_id, json.dumps(profile.config)))
            s = request.scope
            await db.execute("""INSERT INTO rag.document_indexes
                (id,profile_id,owner_id,topic_id,chapter_id,document_id,version_id,source_sha256,request_hash,status,chunk_count)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'BUILDING',%s)""",
                (request.index_id, profile.profile_id, s.owner_id, s.topic_id, s.chapter_id,
                 s.document_id, s.version_id, request.extraction.source_sha256, fingerprint, len(chunks)))
            async with db.cursor() as cursor:
                await cursor.executemany("""INSERT INTO rag.chunks
                    (id,index_id,ordinal,kind,position,start_offset,end_offset,text,text_sha256)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [(c.chunk_id, request.index_id, c.ordinal, c.kind, c.position,
                      c.start_offset, c.end_offset, c.text, c.text_sha256) for c in chunks])
            return await self._scoped(db, request)

    async def claim(self, command: IndexCommand, token: UUID) -> tuple[dict, list[dict]]:
        """Lease one bounded embedding batch and persist its invocation budget first."""
        async with self.pool.connection() as db:
            row = await self._scoped(db, command, lock=True)
            if row["status"] == "READY":
                return row, []
            # A lease is an ownership fence, not a guarantee of exactly-once billing.
            busy = await (await db.execute("SELECT 1 FROM rag.document_indexes WHERE id=%s AND lease_expires_at>clock_timestamp()", (command.index_id,))).fetchone()
            if busy:
                raise RagError("RAG_INDEX_BUSY", 503, True)
            if row["attempt_count"] >= 3 * ((row["chunk_count"] + BATCH_SIZE - 1)//BATCH_SIZE):
                raise RagError("RAG_RETRY_EXHAUSTED", 409)
            await db.execute("""UPDATE rag.document_indexes SET lease_token=%s,
                lease_expires_at=clock_timestamp()+interval '50 seconds',attempt_count=attempt_count+1
                WHERE id=%s""", (token, command.index_id))
            chunks = await (await db.execute("""SELECT id,text FROM rag.chunks WHERE index_id=%s
                AND embedding IS NULL ORDER BY ordinal LIMIT %s""", (command.index_id, BATCH_SIZE))).fetchall()
            return row, chunks

    async def save(self, command: IndexCommand, token: UUID, vectors: list[tuple[UUID,list[float]]]) -> dict:
        """Commit checked vectors and READY atomically under the current lease."""
        async with self.pool.connection() as db:
            await self._scoped(db, command, lock=True)
            lease = await (await db.execute("""SELECT 1 FROM rag.document_indexes WHERE id=%s
                AND lease_token=%s AND lease_expires_at>clock_timestamp()""", (command.index_id, token))).fetchone()
            if not lease:
                raise RagError("RAG_LEASE_LOST", 409)
            async with db.cursor() as cursor:
                await cursor.executemany("UPDATE rag.chunks SET embedding=%s::public.vector WHERE id=%s AND index_id=%s AND embedding IS NULL",
                    [(json.dumps(vector, allow_nan=False), chunk_id, command.index_id) for chunk_id,vector in vectors])
            count = await (await db.execute("SELECT count(*) AS n FROM rag.chunks WHERE index_id=%s AND embedding IS NOT NULL", (command.index_id,))).fetchone()
            await db.execute("""UPDATE rag.document_indexes SET completed_chunks=%s,
                status=CASE WHEN chunk_count=%s THEN 'READY' ELSE 'BUILDING' END,
                lease_token=NULL,lease_expires_at=NULL,updated_at=clock_timestamp() WHERE id=%s""",
                (count["n"], count["n"], command.index_id))
            return await self._scoped(db, command)

    async def release(self, index_id: UUID, token: UUID) -> None:
        async with self.pool.connection() as db:
            await db.execute("UPDATE rag.document_indexes SET lease_token=NULL,lease_expires_at=NULL WHERE id=%s AND lease_token=%s", (index_id, token))

    async def validate_scope(self, owner: UUID, profile: str, indexes: list[UUID]) -> None:
        async with self.pool.connection() as db:
            rows = await (await db.execute("""SELECT id FROM rag.document_indexes
                WHERE id=ANY(%s) AND owner_id=%s AND profile_id=%s AND status='READY'""", (indexes, owner, profile))).fetchall()
            if {row["id"] for row in rows} != set(indexes):
                raise RagError("RAG_SCOPE_UNAVAILABLE", 404)

    async def search(self, owner: UUID, profile: str, indexes: list[UUID], vector: list[float]) -> list[dict]:
        """Exact cosine search with authorization filters applied inside SQL."""
        async with self.pool.connection() as db:
            return await (await db.execute("""SELECT c.id AS chunk_id,c.ordinal,c.kind,c.position,
                c.start_offset,c.end_offset,c.text,c.text_sha256,i.id AS index_id,i.document_id,
                i.version_id,i.source_sha256,1-(c.embedding <=> %s::public.vector) AS similarity
                FROM rag.chunks c JOIN rag.document_indexes i ON i.id=c.index_id
                WHERE i.id=ANY(%s) AND i.owner_id=%s AND i.profile_id=%s AND i.status='READY'
                AND c.embedding IS NOT NULL ORDER BY c.embedding <=> %s::public.vector,c.id LIMIT 40""",
                (json.dumps(vector), indexes, owner, profile, json.dumps(vector)))).fetchall()

    async def revoke(self, command: IndexCommand) -> None:
        """Keep a tombstone so a late prepare cannot resurrect a revoked index."""
        async with self.pool.connection() as db:
            await db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(command.index_id),))
            existing = await (await db.execute("""SELECT profile_id,owner_id,topic_id,chapter_id,document_id,version_id
                FROM rag.document_indexes WHERE id=%s""", (command.index_id,))).fetchone()
            if existing and (existing["profile_id"] != command.profile_id or any(
                existing[field] != value for field, value in command.scope.model_dump().items()
            )):
                raise RagError("RAG_SCOPE_UNAVAILABLE", 404)
            await db.execute("INSERT INTO rag.revoked_indexes(index_id,owner_id,profile_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (command.index_id, command.scope.owner_id, command.profile_id))
            await db.execute("UPDATE rag.document_indexes SET status='REVOKED',lease_token=NULL,lease_expires_at=NULL WHERE id=%s AND owner_id=%s",
                (command.index_id, command.scope.owner_id))
