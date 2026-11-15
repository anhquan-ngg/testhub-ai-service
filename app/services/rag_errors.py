"""Sanitized failures shared by RAG adapters, services and routers."""

from app.schemas.rag import EvidenceDiagnostics


class RagError(Exception):
    def __init__(
        self, code: str, status: int = 422, retryable: bool = False,
        *, diagnostics: EvidenceDiagnostics | None = None,
    ) -> None:
        self.code, self.status, self.retryable = code, status, retryable
        self.diagnostics = diagnostics
        super().__init__(code)
