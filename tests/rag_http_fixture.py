"""Transport-only repository/embedding substitutes. They do not test PostgreSQL."""
from contextlib import asynccontextmanager
from app.services.retrieval import RagService
from app.services.rag_errors import RagError

class Repository:
    def __init__(self):self.rows={};self.chunks={};self.revoked=set()
    async def prepare(self,request,profile,chunks,fingerprint):
        if request.index_id in self.revoked:raise RagError("RAG_SCOPE_UNAVAILABLE",404)
        row={"id":request.index_id,"profile_id":request.profile_id,"status":"BUILDING","chunk_count":len(chunks),"completed_chunks":0,"scope":request.scope,"hash":request.extraction.source_sha256}
        self.rows.setdefault(request.index_id,row);self.chunks.setdefault(request.index_id,chunks)
        return self.rows[request.index_id]
    async def claim(self,request,token):
        row=self.rows[request.index_id]
        return row,[] if row["status"]=="READY" else [{"id":c.chunk_id,"text":c.text} for c in self.chunks[request.index_id]]
    async def save(self,request,token,vectors):
        row=self.rows[request.index_id];row.update(status="READY",completed_chunks=row["chunk_count"]);return row
    async def release(self,*args):pass
    async def validate_scope(self,owner,profile,indexes):
        if any(i not in self.rows or self.rows[i]["scope"].owner_id!=owner or self.rows[i]["profile_id"]!=profile or self.rows[i]["status"]!="READY" for i in indexes):raise RagError("RAG_SCOPE_UNAVAILABLE",404)
    async def search(self,owner,profile,indexes,vector,source_units=None):
        return [{**c.model_dump(),"index_id":i,"document_id":self.rows[i]["scope"].document_id,"version_id":self.rows[i]["scope"].version_id,"source_sha256":self.rows[i]["hash"],"similarity":1.} for i in indexes for c in self.chunks[i] if source_units is None or {"index_id":str(i),"kind":c.kind,"position":c.position} in source_units]
    async def revoke(self,request):
        self.revoked.add(request.index_id)
        if request.index_id in self.rows:self.rows[request.index_id]["status"]="REVOKED"

class Embedding:
    async def embed(self,texts,task):return [[1.]+[0.]*767 for _ in texts]
    async def aclose(self):pass


def enable_rag_fixture(app):
    """Run production routing/chunking/validation with explicit synthetic dependencies."""
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(instance):
        async with original(instance):
            instance.state.rag=RagService(Repository(),Embedding())
            from app.services.grounded_generation import GroundedGeneration
            instance.state.grounded_generator=GroundedGeneration(instance.state.rag,instance.state.generator)
            yield
    app.router.lifespan_context=lifespan
    return app
