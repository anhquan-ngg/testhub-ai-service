"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.generation import router as generation_router
from app.config import Settings
from app.providers.base import ProviderError, QuestionProvider
from app.providers.gemini import GeminiProvider
from app.services.question_generator import QuestionGenerator

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    provider_factory: Callable[[Settings], QuestionProvider] = GeminiProvider,
) -> FastAPI:
    """Build the API with settings and a shared provider owned by its lifespan."""
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config = settings if settings is not None else Settings.from_env()
        app.state.settings = config
        app.state.generator = None
        provider = None
        if config.gemini_api_key:
            try:
                provider = provider_factory(config)
            except ProviderError:
                # The provider has logged sanitized initialization metadata.
                logger.warning("generation_provider_unavailable")
            else:
                app.state.generator = QuestionGenerator(
                    provider, timeout_ms=config.generation_timeout_ms,
                    max_concurrency=config.max_concurrent_generations,
                )
        try:
            yield
        finally:
            if provider is not None:
                await provider.aclose()

    app = FastAPI(title="TestHub AI Service", version="0.1.0", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(generation_router)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Confirm the process is serving requests without calling Gemini."""
        return {"status": "ok"}

    return app


app = create_app()
