"""Application settings loaded once during startup."""

import os
from dataclasses import dataclass, field


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

    def __post_init__(self) -> None:
        if self.rag_enabled and (not self.rag_database_url or not self.gemini_api_key):
            raise ValueError("RAG_ENABLED requires RAG_DATABASE_URL and GEMINI_API_KEY")
        bounds = {
            "gemini_timeout_ms": (1, 120_000),
            "generation_timeout_ms": (1, 180_000),
            "gemini_max_attempts": (1, 5),
            "max_concurrent_generations": (1, 100),
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
        )
