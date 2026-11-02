"""Exercise the 9Router adapter through an in-memory HTTP transport."""

import json
import logging
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers.base import ProviderError, ProviderTimeoutError
from app.providers.factory import create_provider
from app.providers.gemini import GeminiProvider
from app.providers.nine_router import NineRouterProvider

API_KEY = "router-test-secret-key"
SETTINGS = Settings(
    llm_provider="9router",
    router_base_url="http://127.0.0.1:20128/v1",
    router_api_key=API_KEY,
    router_model="test/model",
    internal_api_token="test-only-token",
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


def completion(content: object = QUESTION_JSON, **overrides: object) -> httpx.Response:
    choice: dict[str, object] = {
        "index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": content},
    }
    choice.update(overrides)
    return httpx.Response(200, json={"choices": [choice]})


def build_app(settings: Settings, handler):
    return create_app(
        settings,
        provider_factory=lambda s: NineRouterProvider(
            s, transport=httpx.MockTransport(handler),
        ),
    )


def post(settings: Settings, handler):
    with TestClient(build_app(settings, handler)) as client:
        return client.post("/internal/generate", headers=HEADERS, json=PAYLOAD)


def test_success_sends_expected_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return completion()

    response = post(SETTINGS, handler)
    assert response.status_code == 200
    assert response.json()["questions"][0]["options"][0]["isCorrect"] is True
    request = seen[0]
    assert str(request.url) == "http://127.0.0.1:20128/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    body = json.loads(request.content)
    assert body["model"] == "test/model"
    assert body["stream"] is False and body["n"] == 1
    assert body["max_tokens"] == SETTINGS.router_max_output_tokens
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["messages"][1]["role"] == "user"
    assert PAYLOAD["text"] in body["messages"][1]["content"]


def test_strict_schema_closes_every_object() -> None:
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return completion()

    post(SETTINGS, handler)
    schema = seen[0]["response_format"]["json_schema"]["schema"]

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert node["required"] == list(node["properties"])
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(schema)


def test_json_object_mode_embeds_schema_in_prompt() -> None:
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return completion()

    settings = replace(SETTINGS, router_response_format="json_object")
    assert post(settings, handler).status_code == 200
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert "questions" in seen[0]["messages"][0]["content"]


def test_retries_503_then_succeeds(monkeypatch) -> None:
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.providers.nine_router.asyncio.sleep", no_sleep)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "busy"})
        return completion()

    assert post(replace(SETTINGS, router_max_attempts=3), handler).status_code == 200
    assert calls == 2


def test_retries_are_bounded(monkeypatch) -> None:
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.providers.nine_router.asyncio.sleep", no_sleep)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": "quota"})

    response = post(replace(SETTINGS, router_max_attempts=2), handler)
    assert response.status_code == 502
    assert "9Router" in response.text
    assert calls == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_errors_are_not_retried_and_do_not_leak(status, caplog) -> None:
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": "private-source-material"})

    with caplog.at_level(logging.DEBUG):
        response = post(replace(SETTINGS, router_max_attempts=3), handler)
    assert response.status_code == 502
    assert calls == 1
    for text in (response.text, caplog.text):
        assert "private-source-material" not in text
        assert API_KEY not in text
        assert PAYLOAD["text"] not in text
    assert f"status_code={status}" in caplog.text


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectTimeout])
def test_transport_timeout_returns_504(error) -> None:
    def handler(request):
        raise error("secret source material", request=request)

    response = post(SETTINGS, handler)
    assert response.status_code == 504
    assert "secret" not in response.text
    assert "9Router" in response.text


def test_connect_error_returns_502() -> None:
    def handler(request):
        raise httpx.ConnectError("secret source material", request=request)

    response = post(SETTINGS, handler)
    assert response.status_code == 502
    assert "secret" not in response.text


def test_redirect_is_not_followed() -> None:
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "http://evil.example/"})

    assert post(SETTINGS, handler).status_code == 502
    assert calls == 1


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not json", headers={"content-type": "text/plain"}),
    httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"}),
    httpx.Response(200, json=["list"]),
    httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "a"}},
                                          {"finish_reason": "stop", "message": {"content": "b"}}]}),
    completion(finish_reason="length"),
    completion(content=""),
    completion(content="   "),
    completion(content=None),
    completion(content=["part"]),
    completion(message={"content": QUESTION_JSON, "refusal": "no"}),
    completion(message={"content": QUESTION_JSON, "tool_calls": [{"id": "x"}]}),
    httpx.Response(200, content=b"x" * (1_048_576 + 1),
                   headers={"content-type": "application/json"}),
])
def test_invalid_completions_are_rejected(response) -> None:
    result = post(SETTINGS, lambda request: response)
    assert result.status_code == 502
    assert "9Router" in result.text


def test_non_json_model_content_fails_shared_validation() -> None:
    result = post(SETTINGS, lambda request: completion("this is not json"))
    assert result.status_code == 502


@pytest.mark.anyio
async def test_grounded_generation_uses_grounded_schema() -> None:
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return completion("{}")

    provider = NineRouterProvider(SETTINGS, transport=httpx.MockTransport(handler))
    assert await provider.generate_grounded("prompt") == "{}"
    await provider.aclose()
    assert seen[0]["response_format"]["json_schema"]["name"] == "RawGroundedQuestionList"

@pytest.mark.anyio
async def test_aclose_is_idempotent() -> None:
    provider = NineRouterProvider(
        SETTINGS, transport=httpx.MockTransport(lambda request: completion()),
    )
    await provider.aclose()
    await provider.aclose()
    assert provider._client.is_closed


@pytest.mark.anyio
async def test_provider_errors_are_sanitized() -> None:
    def handler(request):
        raise httpx.ReadTimeout("secret", request=request)

    provider = NineRouterProvider(SETTINGS, transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderTimeoutError) as info:
        await provider.generate("prompt")
    assert "secret" not in str(info.value)
    await provider.aclose()
    provider = NineRouterProvider(
        SETTINGS, transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    with pytest.raises(ProviderError):
        await provider.generate("prompt")
    await provider.aclose()


def test_factory_selects_provider() -> None:
    assert isinstance(create_provider(Settings(gemini_api_key="k")), GeminiProvider)
    provider = create_provider(SETTINGS)
    assert isinstance(provider, NineRouterProvider)


def test_router_starts_without_gemini_key_and_default_unchanged() -> None:
    with TestClient(build_app(SETTINGS, lambda request: completion())) as client:
        assert client.app.state.generator is not None
    with TestClient(create_app(Settings(internal_api_token="test-only-token"))) as client:
        assert client.app.state.generator is None
