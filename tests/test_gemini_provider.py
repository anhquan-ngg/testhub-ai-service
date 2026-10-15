"""Exercise the installed SDK through an in-memory HTTP transport."""

import asyncio
import json
import logging
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from google import genai

from app.config import Settings
from app.main import create_app
from app.providers.base import ProviderTimeoutError
from app.providers.gemini import GeminiProvider


SETTINGS = Settings(
    gemini_api_key="test-key", internal_api_token="test-only-token",
    gemini_max_attempts=1,
)
PAYLOAD = {
    "text": "Binary search requires sorted input and has logarithmic time complexity.",
    "question_format": "KNOWLEDGE", "count": 1,
}
HEADERS = {"X-Internal-Token": "test-only-token"}
QUESTION_JSON = json.dumps({"questions": [{
    "question_text": "What does binary search require?",
    "options": [
        {"text": "Sorted input", "isCorrect": True},
        {"text": "Random input", "isCorrect": False},
        {"text": "A graph", "isCorrect": False},
        {"text": "A queue", "isCorrect": False},
    ],
}]})


def success_response() -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{
        "content": {"parts": [{"text": QUESTION_JSON}], "role": "model"},
        "finishReason": "STOP",
    }]})


def install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list:
    """Keep the real SDK and replace only its outbound HTTP transport."""
    original_client = genai.Client
    clients = []

    def client_factory(**kwargs):
        kwargs["http_options"].async_client_args = {"transport": httpx.MockTransport(handler)}
        client = original_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("app.providers.gemini.genai.Client", client_factory)
    return clients


@pytest.mark.parametrize("timeout_type", [httpx.ReadTimeout, httpx.ConnectTimeout])
def test_sdk_timeout_returns_504(monkeypatch, timeout_type) -> None:
    def handler(request):
        raise timeout_type("secret source material", request=request)

    install_transport(monkeypatch, handler)
    with TestClient(create_app(SETTINGS)) as client:
        response = client.post("/internal/generate", headers=HEADERS, json=PAYLOAD)
    assert response.status_code == 504
    assert "secret" not in response.text


def test_sdk_retries_503_and_reuses_client(monkeypatch) -> None:
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"code": 503, "message": "temporary"}})
        return success_response()

    clients = install_transport(monkeypatch, handler)
    settings = replace(SETTINGS, gemini_max_attempts=3)
    with TestClient(create_app(settings)) as client:
        for _ in range(2):
            assert client.post("/internal/generate", headers=HEADERS, json=PAYLOAD).status_code == 200
        assert len(clients) == 1
        assert not clients[0].aio._api_client._async_httpx_client.is_closed
    assert len(calls) == 3
    assert clients[0].aio._api_client._async_httpx_client.is_closed
    assert clients[0]._api_client._httpx_client.is_closed
    assert calls[0]["generationConfig"]["responseMimeType"] == "application/json"


def test_retries_are_bounded(monkeypatch) -> None:
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": {"code": 429, "message": "quota"}})

    install_transport(monkeypatch, handler)
    with TestClient(create_app(replace(SETTINGS, gemini_max_attempts=2))) as client:
        assert client.post("/internal/generate", headers=HEADERS, json=PAYLOAD).status_code == 502
    assert calls == 2


def test_permanent_error_is_not_retried_and_logs_only_metadata(monkeypatch, caplog) -> None:
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"error": {
            "code": 403, "message": "secret-key-and-private-source",
        }})

    install_transport(monkeypatch, handler)
    with caplog.at_level(logging.WARNING, logger="app.providers.gemini"):
        with TestClient(create_app(replace(SETTINGS, gemini_max_attempts=3))) as client:
            response = client.post("/internal/generate", headers=HEADERS, json=PAYLOAD)
    assert calls == 1
    assert response.status_code == 502
    assert "status_code=403" in caplog.text
    assert "exception_type=ClientError" in caplog.text
    assert "secret-key-and-private-source" not in caplog.text + response.text
    assert all(record.exc_info is None for record in caplog.records)


def test_overall_deadline_cancels_sdk_and_releases_slot(monkeypatch) -> None:
    calls = 0
    cancelled = []

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return success_response()

    install_transport(monkeypatch, handler)
    settings = replace(SETTINGS, generation_timeout_ms=100, max_concurrent_generations=1)
    with TestClient(create_app(settings)) as client:
        assert client.post("/internal/generate", headers=HEADERS, json=PAYLOAD).status_code == 504
        assert client.post("/internal/generate", headers=HEADERS, json=PAYLOAD).status_code == 200
    assert cancelled == [True]


def test_constructor_errors_are_sanitized(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "app.providers.gemini.genai.Client", Mock(side_effect=ValueError("secret-in-constructor")),
    )
    with TestClient(create_app(SETTINGS)) as client:
        response = client.post("/internal/generate", headers=HEADERS, json=PAYLOAD)
        assert client.get("/health").status_code == 200
    assert response.status_code == 503
    assert "exception_type=ValueError" in caplog.text
    assert "secret-in-constructor" not in caplog.text + response.text


def test_cleanup_failures_cannot_replace_timeout(monkeypatch, caplog) -> None:
    sdk_client = Mock()
    sdk_client.aio.models.generate_content = AsyncMock(side_effect=httpx.ReadTimeout("secret"))
    sdk_client.aio.aclose = AsyncMock(side_effect=RuntimeError("secret-async-close"))
    sdk_client.close.side_effect = RuntimeError("secret-sync-close")
    monkeypatch.setattr("app.providers.gemini.genai.Client", Mock(return_value=sdk_client))

    async def run():
        provider = GeminiProvider(SETTINGS)
        try:
            with pytest.raises(ProviderTimeoutError):
                await provider.generate("prompt")
        finally:
            await provider.aclose()
            await provider.aclose()

    asyncio.run(run())
    sdk_client.aio.aclose.assert_awaited_once()
    sdk_client.close.assert_called_once()
    assert "stage=async_close" in caplog.text
    assert "stage=sync_close" in caplog.text
    assert "secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
