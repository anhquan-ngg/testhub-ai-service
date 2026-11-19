"""Shutdown attempts every initialized resource without masking its first failure."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.main import create_app


@pytest.mark.parametrize("first_failure", ["parser", "embedding", "provider", "cancelled", None])
def test_shutdown_closes_all_resources_and_preserves_first_error(monkeypatch, first_failure):
    """All close callbacks run even when earlier cleanup fails or is cancelled."""
    events = []
    failures = {
        "parser": RuntimeError("parser failure"),
        "embedding": RuntimeError("embedding failure"),
        "repository": RuntimeError("repository failure"),
        "provider": RuntimeError("provider failure"),
    }
    if first_failure == "cancelled":
        failures["parser"] = asyncio.CancelledError()
    order = ["parser", "embedding", "repository", "provider"]
    start = order.index("parser" if first_failure == "cancelled" else first_failure) if first_failure else len(order)

    def resource(name, close_name):
        async def close():
            events.append(name)
            if order.index(name) >= start:
                raise failures[name]
        return SimpleNamespace(**{close_name: close, "open": AsyncMock()})

    parser = resource("parser", "aclose")
    embedding = resource("embedding", "aclose")
    repository = resource("repository", "close")
    provider = resource("provider", "aclose")
    monkeypatch.setattr("app.main.DocumentParser", lambda: parser)
    monkeypatch.setattr("app.services.embedding.GoogleEmbeddingProvider", lambda *args, **kwargs: embedding)
    monkeypatch.setattr("app.repositories.vector_repository.VectorRepository", lambda *args: repository)
    monkeypatch.setattr("app.repositories.windows_pool.SelectorRepository", lambda: repository)
    monkeypatch.setattr("app.services.retrieval.RagService", lambda *args: SimpleNamespace())
    monkeypatch.setattr("app.services.grounded_generation.GroundedGeneration", lambda *args, **kwargs: SimpleNamespace())
    settings = Settings(
        rag_enabled=True, rag_database_url="postgresql://fixture", gemini_api_key="fixture-key",
    )
    app = create_app(settings, provider_factory=lambda _: provider)

    async def run():
        if first_failure:
            expected = failures["parser" if first_failure == "cancelled" else first_failure]
            with pytest.raises(type(expected)) as caught:
                async with app.router.lifespan_context(app):
                    pass
            assert caught.value is expected
        else:
            async with app.router.lifespan_context(app):
                pass
        assert events == order

    asyncio.run(run())
