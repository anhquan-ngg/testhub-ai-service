"""Validate the versioned NestJS fixture against the real FastAPI boundary."""

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas.question import GenerateRequest, GenerateResponse


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "contracts" / "text-generation.v1.json"
TEST_TOKEN = "contract-only-internal-token-123456789"


class ContractProvider:
    """Exercise prompt/validation without a paid provider or private config."""

    def __init__(self, fixture: dict) -> None:
        self.fixture = fixture

    async def generate(self, prompt: str) -> str:
        questions = self.fixture["internal_response"]["questions"]
        return json.dumps({"questions": [
            {"question_text": question["question_text"], "options": question["options"]}
            for question in questions
        ]}, ensure_ascii=False)

    async def generate_grounded(self, prompt: str) -> str:
        data=json.loads(prompt.split("Reference JSON follows:\n",1)[1]);chunk=data["excerpts"][0]
        return json.dumps({"questions":[{"question_text":"Which principle does a stack use?","options":[{"text":text,"isCorrect":i==0} for i,text in enumerate(["LIFO","FIFO","Random","Sorted"])],"citations":[{"chunk_id":chunk["chunk_id"],"quote":chunk["text"]}]}]})

    async def aclose(self) -> None:
        pass


def build_contract_app() -> FastAPI:
    """Build the actual production routes with deterministic generation."""
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return create_app(
        Settings(gemini_api_key="fixture-only", internal_api_token=TEST_TOKEN),
        provider_factory=lambda config: ContractProvider(fixture),
    )


def test_versioned_nestjs_contract() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["version"] == "text-generation.v1"
    assert len(GenerateRequest.model_validate(fixture["internal_request"]).slots()) == 1
    GenerateResponse.model_validate_json(json.dumps(fixture["internal_response"]))
    with TestClient(build_contract_app()) as client:
        response = client.post(
            "/internal/generate", json=fixture["internal_request"],
            headers={"X-Internal-Token": TEST_TOKEN},
        )
        assert response.status_code == 200
        assert response.json() == fixture["internal_response"]


def test_nestjs_token_mismatch_is_unauthorized() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    with TestClient(build_contract_app()) as client:
        response = client.post(
            "/internal/generate", json=fixture["internal_request"],
            headers={"X-Internal-Token": "wrong-test-token"},
        )
        assert response.status_code == 401
