"""FastAPI application entry point."""

import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.generation import router as generation_router
from app.config import Settings
from app.providers.base import ProviderError, QuestionProvider
from app.providers.factory import create_provider
from app.services.question_generator import QuestionGenerator
from app.api.documents import router as documents_router
from app.services.document_parser import DocumentParser
from app.api.rag import router as rag_router, RagBodyLimit

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    provider_factory: Callable[[Settings], QuestionProvider] = create_provider,
) -> FastAPI:
    """Build the API with settings and a shared provider owned by its lifespan."""
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config = settings if settings is not None else Settings.from_env()
        app.state.settings = config
        app.state.generator = None
        document_parser = app.state.document_parser = DocumentParser()
        app.state.rag = None
        app.state.grounded_generator = None
        rag_repository = embedding = None
        if config.rag_enabled:
            try:
                from app.repositories.vector_repository import VectorRepository
                from app.services.embedding import GoogleEmbeddingProvider
                from app.services.retrieval import RagService
                embedding = GoogleEmbeddingProvider(
                    config.effective_embedding_api_key,
                    max_rpm=config.embedding_max_rpm,
                    max_concurrency=config.embedding_max_concurrency,
                    max_attempts=config.embedding_max_attempts,
                    max_backoff_ms=config.embedding_max_backoff_ms,
                    retry_budget_ms=config.embedding_retry_budget_ms,
                    cache_size=config.embedding_cache_size,
                    indexing_rpm_share=config.embedding_indexing_rpm_percent / 100,
                    indexing_max_concurrency=config.embedding_indexing_max_concurrency,
                    daily_limit=config.embedding_daily_limit,
                    daily_warn_percent=config.embedding_daily_warn_percent,
                    daily_indexing_percent=config.embedding_daily_indexing_percent,
                )
                if sys.platform == "win32":
                    from app.repositories.windows_pool import SelectorRepository
                    rag_repository = SelectorRepository()
                    await rag_repository.open(lambda: VectorRepository(config.rag_database_url))
                else:
                    rag_repository = VectorRepository(config.rag_database_url)
                    await rag_repository.open()
                if config.embedding_persistent_cache_enabled:
                    from app.repositories.embedding_cache import RepositoryEmbeddingCache
                    embedding.attach_persistent_cache(RepositoryEmbeddingCache(
                        rag_repository, config.embedding_persistent_cache_ttl_days))
                app.state.rag = RagService(rag_repository, embedding)
            except Exception:
                logger.warning("rag_initialization_unavailable")
        provider = None
        if config.llm_provider == "9router" or config.gemini_api_key:
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
        if app.state.rag is not None and app.state.generator is not None:
            from app.services.grounded_generation import GroundedGeneration
            app.state.grounded_generator = GroundedGeneration(
                app.state.rag, app.state.generator, timeout_ms=config.effective_grounded_timeout_ms,
            )
        try:
            yield
        finally:
            cleanup_error: BaseException | None = None
            resources = [(document_parser.aclose, False)]
            if embedding is not None:
                resources.append((embedding.aclose, False))
            if rag_repository is not None:
                resources.append((rag_repository.close, True))
            if provider is not None:
                resources.append((provider.aclose, False))
            for close, best_effort in resources:
                try:
                    await close()
                except BaseException as exc:
                    if best_effort and isinstance(exc, Exception):
                        logger.warning("rag_pool_close_failed")
                    elif cleanup_error is None:
                        cleanup_error = exc
            if cleanup_error is not None:
                raise cleanup_error

    app = FastAPI(title="TestHub AI Service", version="0.1.0", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(generation_router)
    app.include_router(documents_router)
    app.include_router(rag_router)
    app.add_middleware(RagBodyLimit)

    @app.get("/ready", tags=["health"])
    async def ready():
        """Check technical storage/schema without making a paid API call."""
        from fastapi.responses import JSONResponse
        if not app.state.settings.rag_enabled:
            return {"status": "ok", "rag": "disabled"}
        try:
            if app.state.rag is not None and await app.state.rag.repository.ready():
                return {"status": "ok", "rag": "ready"}
        except Exception:
            pass
        return JSONResponse(status_code=503, content={"status": "unavailable", "rag": "unavailable"})

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Confirm the process is serving requests without calling an upstream."""
        return {"status": "ok"}

    return app


app = create_app()
