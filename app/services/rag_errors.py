"""Sanitized failures shared by RAG adapters, services and routers."""


class RagError(Exception):
    def __init__(self, code: str, status: int = 422, retryable: bool = False) -> None:
        self.code, self.status, self.retryable = code, status, retryable
        super().__init__(code)
