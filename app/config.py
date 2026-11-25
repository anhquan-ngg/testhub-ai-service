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
    llm_fallback_provider: str = ""
    grounded_timeout_ms: int = 0  # 0 = derive from generation_timeout_ms
    embedding_max_rpm: int = 60
    embedding_max_concurrency: int = 2
    embedding_max_attempts: int = 4
    embedding_max_backoff_ms: int = 8_000
    embedding_retry_budget_ms: int = 15_000
    embedding_cache_size: int = 2_048
    embedding_api_key: str = field(default="", repr=False)  # empty = reuse gemini_api_key
    embedding_indexing_rpm_percent: int = 50  # share of EMBEDDING_MAX_RPM that indexing may use
    embedding_indexing_max_concurrency: int = 1
    embedding_persistent_cache_enabled: bool = False
    embedding_persistent_cache_ttl_days: int = 30
    embedding_daily_limit: int = 0  # 0 = off; local per-process requests/day guard
    embedding_daily_warn_percent: int = 80
    embedding_daily_indexing_percent: int = 80  # indexing stops here so queries keep headroom
    ocr_enabled: bool = False  # hybrid extraction: vision OCR for thin/scanned pages via 9router
    ocr_model: str = ""
    ocr_max_pages: int = 30  # per document; pages beyond the cap keep their text layer only
    ocr_max_concurrency: int = 3
    ocr_max_attempts: int = 2
    ocr_timeout_ms: int = 30_000  # per vision call
    ocr_time_budget_ms: int = 35_000  # per document; unfinished pages stay unrecovered
    ocr_max_output_tokens: int = 4_096
    ocr_dpi: int = 150
    ocr_max_image_bytes: int = 4_000_000
    ocr_daily_page_limit: int = 0  # 0 = off; local per-process vision calls/day guard
    ocr_persistent_cache_enabled: bool = False
    ocr_cache_ttl_days: int = 90
    ocr_config_version: str = "v1"  # bump to invalidate cached transcripts

    def __post_init__(self) -> None:
        if self.llm_provider not in ("gemini", "9router"):
            raise ValueError("LLM_PROVIDER must be gemini or 9router")
        if self.llm_fallback_provider not in ("", "gemini"):
            raise ValueError("LLM_FALLBACK_PROVIDER must be empty or gemini")
        if self.llm_fallback_provider == "gemini":
            if self.llm_provider == "gemini":
                raise ValueError("LLM_FALLBACK_PROVIDER must differ from LLM_PROVIDER")
            if not self.gemini_api_key:
                raise ValueError("LLM_FALLBACK_PROVIDER=gemini requires GEMINI_API_KEY")
        if self.router_response_format not in ("json_schema", "json_object"):
            raise ValueError("ROUTER_RESPONSE_FORMAT must be json_schema or json_object")
        if self.llm_provider == "9router" or self.ocr_enabled:
            self._validate_router_endpoint()
        if self.llm_provider == "9router":
            self._validate_model(self.router_model, "ROUTER_MODEL")
        if self.ocr_enabled:
            self._validate_model(self.ocr_model, "OCR_MODEL")
        if self.rag_enabled and (not self.rag_database_url or not self.effective_embedding_api_key):
            raise ValueError("RAG_ENABLED requires RAG_DATABASE_URL and EMBEDDING_GOOGLE_API_KEY or GEMINI_API_KEY")
        bounds = {
            "gemini_timeout_ms": (1, 120_000),
            "generation_timeout_ms": (1, 180_000),
            "gemini_max_attempts": (1, 5),
            "max_concurrent_generations": (1, 100),
            "router_timeout_ms": (1, 120_000),
            "router_max_attempts": (1, 5),
            "router_max_output_tokens": (1, 65_536),
            "grounded_timeout_ms": (0, 300_000),
            "embedding_max_rpm": (1, 10_000),
            "embedding_max_concurrency": (1, 16),
            "embedding_max_attempts": (1, 6),
            "embedding_max_backoff_ms": (0, 30_000),
            # Must stay well under RagService's 40s operation deadline.
            "embedding_retry_budget_ms": (0, 30_000),
            "embedding_cache_size": (0, 100_000),
            "embedding_indexing_rpm_percent": (1, 100),
            "embedding_indexing_max_concurrency": (1, 16),
            "embedding_persistent_cache_ttl_days": (1, 365),
            "embedding_daily_limit": (0, 10_000_000),
            "embedding_daily_warn_percent": (1, 100),
            "embedding_daily_indexing_percent": (1, 100),
            "ocr_max_pages": (1, 200),
            "ocr_max_concurrency": (1, 8),
            "ocr_max_attempts": (1, 4),
            "ocr_timeout_ms": (1_000, 120_000),
            "ocr_time_budget_ms": (1_000, 120_000),
            "ocr_max_output_tokens": (256, 16_384),
            "ocr_dpi": (72, 300),
            "ocr_max_image_bytes": (100_000, 7_000_000),
            "ocr_daily_page_limit": (0, 10_000_000),
            "ocr_cache_ttl_days": (1, 365),
        }
        for name, (minimum, maximum) in bounds.items():
            if not minimum <= getattr(self, name) <= maximum:
                env_name = "GROUNDED_GENERATION_TIMEOUT_MS" if name == "grounded_timeout_ms" else name.upper()
                raise ValueError(f"{env_name} must be between {minimum} and {maximum}")
        if self.grounded_timeout_ms and self.grounded_timeout_ms < self.generation_timeout_ms + 15_000:
            raise ValueError("GROUNDED_GENERATION_TIMEOUT_MS must be at least GENERATION_TIMEOUT_MS + 15000")

    def _validate_router_endpoint(self) -> None:
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

    @staticmethod
    def _validate_model(model: str, env_name: str) -> None:
        if not model.strip() or any(ord(c) < 32 or ord(c) == 127 for c in model):
            raise ValueError(f"{env_name} must be a nonempty model ID without control characters")

    @property
    def effective_embedding_api_key(self) -> str:
        """Dedicated embedding key (ideally from its own Google project), else the shared Gemini key."""
        return self.embedding_api_key or self.gemini_api_key

    @property
    def effective_grounded_timeout_ms(self) -> int:
        """Outer RAG deadline: never below the LLM deadline plus retrieval/scope-check headroom."""
        return self.grounded_timeout_ms or max(75_000, self.generation_timeout_ms + 15_000)

    @classmethod
    def from_env(cls) -> "Settings":
        """Read supported env values without including secrets in errors."""
        def integer(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except ValueError:
                raise ValueError(f"{name} must be an integer") from None

        def flag(name: str) -> bool:
            value = os.getenv(name, "false")
            if value not in ("true", "false"):
                raise ValueError(f"{name} must be true or false")
            return value == "true"

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
            llm_fallback_provider=os.getenv("LLM_FALLBACK_PROVIDER", "").strip(),
            grounded_timeout_ms=integer("GROUNDED_GENERATION_TIMEOUT_MS", 0),
            embedding_max_rpm=integer("EMBEDDING_MAX_RPM", 60),
            embedding_max_concurrency=integer("EMBEDDING_MAX_CONCURRENCY", 2),
            embedding_max_attempts=integer("EMBEDDING_MAX_ATTEMPTS", 4),
            embedding_max_backoff_ms=integer("EMBEDDING_MAX_BACKOFF_MS", 8_000),
            embedding_retry_budget_ms=integer("EMBEDDING_RETRY_BUDGET_MS", 15_000),
            embedding_cache_size=integer("EMBEDDING_CACHE_SIZE", 2_048),
            embedding_api_key=os.getenv("EMBEDDING_GOOGLE_API_KEY", "").strip(),
            embedding_indexing_rpm_percent=integer("EMBEDDING_INDEXING_RPM_PERCENT", 50),
            embedding_indexing_max_concurrency=integer("EMBEDDING_INDEXING_MAX_CONCURRENCY", 1),
            embedding_persistent_cache_enabled=flag("EMBEDDING_PERSISTENT_CACHE_ENABLED"),
            embedding_persistent_cache_ttl_days=integer("EMBEDDING_PERSISTENT_CACHE_TTL_DAYS", 30),
            embedding_daily_limit=integer("EMBEDDING_DAILY_LIMIT", 0),
            embedding_daily_warn_percent=integer("EMBEDDING_DAILY_WARN_PERCENT", 80),
            embedding_daily_indexing_percent=integer("EMBEDDING_DAILY_INDEXING_PERCENT", 80),
            ocr_enabled=flag("OCR_ENABLED"),
            ocr_model=os.getenv("OCR_MODEL", "").strip(),
            ocr_max_pages=integer("OCR_MAX_PAGES", 30),
            ocr_max_concurrency=integer("OCR_MAX_CONCURRENCY", 3),
            ocr_max_attempts=integer("OCR_MAX_ATTEMPTS", 2),
            ocr_timeout_ms=integer("OCR_TIMEOUT_MS", 30_000),
            ocr_time_budget_ms=integer("OCR_TIME_BUDGET_MS", 35_000),
            ocr_max_output_tokens=integer("OCR_MAX_OUTPUT_TOKENS", 4_096),
            ocr_dpi=integer("OCR_DPI", 150),
            ocr_max_image_bytes=integer("OCR_MAX_IMAGE_BYTES", 4_000_000),
            ocr_daily_page_limit=integer("OCR_DAILY_PAGE_LIMIT", 0),
            ocr_persistent_cache_enabled=flag("OCR_PERSISTENT_CACHE_ENABLED"),
            ocr_cache_ttl_days=integer("OCR_CACHE_TTL_DAYS", 90),
            ocr_config_version=os.getenv("OCR_CONFIG_VERSION", "v1").strip() or "v1",
        )
