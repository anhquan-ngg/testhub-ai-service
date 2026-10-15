"""Contract tests for the internal generation endpoint without paid API calls."""

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers.base import ProviderError, ProviderTimeoutError


class FakeProvider:
    def __init__(self, result: str | Exception) -> None:
        self.result = result
        self.prompt: str | None = None

    async def generate(self, prompt: str) -> str:
        self.prompt = prompt
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        pass


def question(text: str = "Độ phức tạp của tìm kiếm nhị phân là gì?") -> dict:
    return {
        "question_text": text,
        "options": [
            {"text": "O(1)", "isCorrect": False},
            {"text": "O(log n)", "isCorrect": True},
            {"text": "O(n)", "isCorrect": False},
            {"text": "O(n²)", "isCorrect": False},
        ],
    }


@pytest.fixture
def client() -> Iterator[TestClient]:
    provider = FakeProvider(
        json.dumps({"questions": [question()]}, ensure_ascii=False)
    )
    app = create_app(
        Settings(gemini_api_key="test-key", internal_api_token="test-only-token"),
        provider_factory=lambda config: provider,
    )
    with TestClient(app) as session:
        yield session


def valid_payload() -> dict:
    return {
        "text": "Tìm kiếm nhị phân hoạt động trên mảng đã sắp xếp và có độ phức tạp O(log n).",
        "topic": "Cấu trúc dữ liệu",
        "question_format": "KNOWLEDGE",
        "count": 1,
    }


def test_returns_testhub_question_shape(client: TestClient) -> None:
    response = client.post(
        "/internal/generate",
        headers={"X-Internal-Token": "test-only-token"},
        json=valid_payload(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "questions": [
            {
                **question(),
                "question_type": "SINGLE_CHOICE",
                "question_format": "KNOWLEDGE",
                "correct_answer": None,
            }
        ]
    }


def test_requires_internal_token(client: TestClient) -> None:
    assert client.post("/internal/generate", json=valid_payload()).status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"count": 0},
        {"count": 21},
        {"count": "1"},
        {"question_format": "UNKNOWN"},
        {"text": "too short"},
    ],
)
def test_rejects_invalid_input(client: TestClient, change: dict) -> None:
    response = client.post(
        "/internal/generate",
        headers={"X-Internal-Token": "test-only-token"},
        json={**valid_payload(), **change},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "result",
    [
        "not json",
        json.dumps({"questions": []}),
        json.dumps({"questions": [{**question(), "options": question()["options"][:3]}]}),
        json.dumps({"questions": [{**question(), "options": [
            {"text": "A", "isCorrect": True},
            {"text": "B", "isCorrect": True},
            {"text": "C", "isCorrect": False},
            {"text": "D", "isCorrect": False},
        ]}]}),
        json.dumps({"questions": [{**question(), "options": [
            {"text": "A", "isCorrect": True},
            {"text": "A", "isCorrect": False},
            {"text": "C", "isCorrect": False},
            {"text": "D", "isCorrect": False},
        ]}]}),
    ],
)
def test_rejects_invalid_model_output(client: TestClient, result: str) -> None:
    client.app.state.generator.provider = FakeProvider(result)
    response = client.post(
        "/internal/generate",
        headers={"X-Internal-Token": "test-only-token"},
        json=valid_payload(),
    )
    assert response.status_code == 502


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [(ProviderError("failed"), 502), (ProviderTimeoutError("timeout"), 504)],
)
def test_maps_provider_failures(
    client: TestClient, error: Exception, expected_status: int
) -> None:
    client.app.state.generator.provider = FakeProvider(error)
    response = client.post(
        "/internal/generate",
        headers={"X-Internal-Token": "test-only-token"},
        json=valid_payload(),
    )
    assert response.status_code == expected_status


def test_health_does_not_require_gemini(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("topic", [None, "", "  \t\n"])
def test_blank_optional_topic(client: TestClient, topic: str | None) -> None:
    response = client.post(
        "/internal/generate", headers={"X-Internal-Token": "test-only-token"},
        json={**valid_payload(), "topic": topic},
    )
    assert response.status_code == 200
    assert '"topic": null' in client.app.state.generator.provider.prompt


def test_non_ascii_header_is_unauthorized(client: TestClient) -> None:
    response = client.post(
        "/internal/generate", headers=[(b"x-internal-token", b"\xff")], json=valid_payload(),
    )
    assert response.status_code == 401


def test_non_ascii_config_token_does_not_crash() -> None:
    app = create_app(Settings(internal_api_token="khóa-bí-mật"))
    with TestClient(app) as client:
        response = client.post(
            "/internal/generate", headers={"X-Internal-Token": "wrong"}, json=valid_payload(),
        )
    assert response.status_code == 401


def test_settings_are_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERNAL_API_TOKEN", "initial")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with TestClient(create_app()) as client:
        monkeypatch.setenv("INTERNAL_API_TOKEN", "changed")
        assert client.post(
            "/internal/generate", headers={"X-Internal-Token": "initial"}, json=valid_payload(),
        ).status_code == 503  # Authentication succeeds; the Gemini key is absent.
        assert client.post(
            "/internal/generate", headers={"X-Internal-Token": "changed"}, json=valid_payload(),
        ).status_code == 401
