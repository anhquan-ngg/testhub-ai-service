"""Regression checks for intake slots and the documented RAG error boundary."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app.api.rag import RagBodyLimit
from app.config import Settings
from app.main import create_app
from app.schemas.rag import RagErrorResponse


def intake_scope(token="fixture-token", configured="fixture-token"):
    return {
        "type": "http", "path": "/internal/rag/step",
        "headers": [] if token is None else [(b"x-internal-token", token.encode("latin-1"))],
        "app": SimpleNamespace(state=SimpleNamespace(settings=Settings(internal_api_token=configured))),
    }


@pytest.mark.parametrize("token,configured,status,code", [
    (None, "fixture-token", 401, "RAG_UNAUTHORIZED"),
    ("wrong", "fixture-token", 401, "RAG_UNAUTHORIZED"),
    ("nonascii-\xe9", "fixture-token", 401, "RAG_UNAUTHORIZED"),
    ("fixture-token", "", 503, "RAG_AUTH_UNAVAILABLE"),
])
def test_auth_rejects_before_reading_body_or_acquiring_capacity(token, configured, status, code):
    async def run():
        app, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
        limiter = RagBodyLimit(app)
        limiter.slots = SimpleNamespace(acquire=AsyncMock(), release=Mock())
        limiter.bounded = AsyncMock()
        await limiter(intake_scope(token, configured), receive, send)
        receive.assert_not_awaited()
        limiter.slots.acquire.assert_not_awaited()
        limiter.slots.release.assert_not_called()
        limiter.bounded.assert_not_awaited()
        app.assert_not_awaited()
        messages = [call.args[0] for call in send.await_args_list]
        assert messages[0]["status"] == status
        assert json.loads(messages[1]["body"]) == {"detail": {"code": code, "retryable": False}}
    asyncio.run(run())


def test_intake_slots_are_released_before_slow_application_work():
    async def run():
        entered = 0
        all_entered = asyncio.Event()
        finish = asyncio.Event()

        async def app(scope, receive, send):
            nonlocal entered
            assert (await receive())["body"] == b"{}"
            entered += 1
            if entered == 5:
                all_entered.set()
            await finish.wait()

        limiter = RagBodyLimit(app)
        responses = []
        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}
        async def send(message):
            responses.append(message)
        tasks = [asyncio.create_task(limiter(intake_scope(), receive, send)) for _ in range(5)]
        try:
            await asyncio.wait_for(all_entered.wait(), 1)
            assert limiter.slots._value == 4
            assert not responses
        finally:
            finish.set()
            await asyncio.gather(*tasks)
    asyncio.run(run())


@pytest.mark.parametrize("mode,status", [("large", 413), ("timeout", 408), ("disconnect", None)])
def test_failed_intake_never_calls_application(monkeypatch, mode, status):
    async def run():
        called = False
        async def app(*args):
            nonlocal called
            called = True
        limiter = RagBodyLimit(app)
        async def receive():
            if mode == "timeout":
                await asyncio.sleep(1)
            if mode == "disconnect":
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": b"x" * (8 * 1024 * 1024 + 1)}
        responses = []
        async def send(message):
            responses.append(message)
        original = asyncio.timeout
        if mode == "timeout":
            monkeypatch.setattr(asyncio, "timeout", lambda _: original(.01))
        await limiter(intake_scope(), receive, send)
        assert not called
        assert limiter.slots._value == 4
        assert [message["status"] for message in responses if message["type"] == "http.response.start"] == ([] if status is None else [status])
    asyncio.run(run())


@pytest.mark.parametrize("path", ["generate", "profile", "prepare", "step", "retrieve", "revoke"])
def test_rag_openapi_and_auth_use_shared_error_shape(path):
    with TestClient(create_app(Settings(internal_api_token="fixture-token"))) as client:
        response = client.get(f"/internal/rag/{path}") if path == "profile" else client.post(f"/internal/rag/{path}", json={})
        assert response.status_code == 401
        assert RagErrorResponse.model_validate(response.json()).detail.retryable is False
        operation = client.app.openapi()["paths"][f"/internal/rag/{path}"]["get" if path == "profile" else "post"]
        for status in (401, 404, 408, 409, 413, 422, 502, 503, 504):
            schema = operation["responses"][str(status)]["content"]["application/json"]["schema"]
            assert schema == {"$ref": "#/components/schemas/RagErrorResponse"}


def test_missing_token_configuration_uses_rag_error_shape():
    with TestClient(create_app(Settings())) as client:
        response = client.get("/internal/rag/profile")
        assert response.status_code == 503
        assert RagErrorResponse.model_validate(response.json()).detail.code == "RAG_AUTH_UNAVAILABLE"
