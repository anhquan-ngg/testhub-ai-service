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

    def __post_init__(self) -> None:
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

        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            internal_api_token=os.getenv("INTERNAL_API_TOKEN", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip(),
            gemini_timeout_ms=integer("GEMINI_TIMEOUT_MS", 30_000),
            generation_timeout_ms=integer("GENERATION_TIMEOUT_MS", 30_000),
            gemini_max_attempts=integer("GEMINI_MAX_ATTEMPTS", 3),
            max_concurrent_generations=integer("MAX_CONCURRENT_GENERATIONS", 4),
        )
