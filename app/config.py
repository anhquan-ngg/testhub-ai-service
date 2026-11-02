"""Application settings loaded once during startup."""

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = field(default="", repr=False)
    internal_api_token: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_timeout_ms: int = 30_000
    generation_timeout_ms: int = 30_000
    gemini_max_attempts: int = 3
    max_concurrent_generations: int = 4
    rag_enabled: bool = False
    rag_database_url: str = field(default="", repr=False)
    llm_provider: str = "gemini"
    router_base_url: str = ""
    router_api_key: str = field(default="", repr=False)
    router_model: str = ""
    router_timeout_ms: int = 30_000
    router_max_attempts: int = 1
    router_max_output_tokens: int = 16_384
    router_response_format: str = "json_schema"

    def __post_init__(self) -> None:
        if self.llm_provider not in ("gemini", "9router"):
            raise ValueError("LLM_PROVIDER must be gemini or 9router")
        if self.router_response_format not in ("json_schema", "json_object"):
            raise ValueError("ROUTER_RESPONSE_FORMAT must be json_schema or json_object")
        if self.llm_provider == "9router":
            try:
                url = urlsplit(self.router_base_url)
                valid_url = (
                    url.scheme in ("http", "https") and bool(url.hostname)
                    and url.username is None and url.password is None
                    and url.port != 0 and url.path.rstrip("/").endswith("/v1")
                    and not any(c.isspace() or ord(c) < 32 for c in self.router_base_url)
                    and not any(c in self.router_base_url for c in ("?", "#", "\\"))
                )
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ValueError("ROUTER_BASE_URL must be an HTTP(S) API base ending in /v1 without credentials, query or fragment")
            if not self.router_api_key or not all(33 <= ord(c) <= 126 for c in self.router_api_key):
                raise ValueError("ROUTER_API_KEY must contain visible ASCII characters")
            if not self.router_model.strip() or any(ord(c) < 32 or ord(c) == 127 for c in self.router_model):
                raise ValueError("ROUTER_MODEL must be a nonempty model ID without control characters")
        if self.rag_enabled and (not self.rag_database_url or not self.gemini_api_key):
            raise ValueError("RAG_ENABLED requires RAG_DATABASE_URL and GEMINI_API_KEY")
        bounds = {
            "gemini_timeout_ms": (1, 120_000),
            "generation_timeout_ms": (1, 180_000),
            "gemini_max_attempts": (1, 5),
            "max_concurrent_generations": (1, 100),
            "router_timeout_ms": (1, 120_000),
            "router_max_attempts": (1, 5),
            "router_max_output_tokens": (1, 65_536),
        }
        for name, (minimum, maximum) in bounds.items():
            if not minimum <= getattr(self, name) <= maximum:
                raise ValueError(f"{name.upper()} must be between {minimum} and {maximum}")

    @classmethod
    def from_env(cls) -> "Settings":
        """Read supported env values without including secrets in errors."""
        def integer(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except ValueError:
                raise ValueError(f"{name} must be an integer") from None

        rag_flag = os.getenv("RAG_ENABLED", "false")
        if rag_flag not in ("true", "false"):
            raise ValueError("RAG_ENABLED must be true or false")
        return cls(
            rag_enabled=rag_flag == "true",
            rag_database_url=os.getenv("RAG_DATABASE_URL", ""),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            internal_api_token=os.getenv("INTERNAL_API_TOKEN", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip(),
            gemini_timeout_ms=integer("GEMINI_TIMEOUT_MS", 30_000),
            generation_timeout_ms=integer("GENERATION_TIMEOUT_MS", 30_000),
            gemini_max_attempts=integer("GEMINI_MAX_ATTEMPTS", 3),
            max_concurrent_generations=integer("MAX_CONCURRENT_GENERATIONS", 4),
            llm_provider=os.getenv("LLM_PROVIDER", "gemini").strip(),
            router_base_url=os.getenv("ROUTER_BASE_URL", "").strip(),
            router_api_key=os.getenv("ROUTER_API_KEY", "").strip(),
            router_model=os.getenv("ROUTER_MODEL", "").strip(),
            router_timeout_ms=integer("ROUTER_TIMEOUT_MS", 30_000),
            router_max_attempts=integer("ROUTER_MAX_ATTEMPTS", 1),
            router_max_output_tokens=integer("ROUTER_MAX_OUTPUT_TOKENS", 16_384),
            router_response_format=os.getenv("ROUTER_RESPONSE_FORMAT", "json_schema").strip(),
        )
