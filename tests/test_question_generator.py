"""Concurrency and cancellation behavior of the shared generation service."""

import asyncio
import json

import pytest

from app.errors import ServiceUnavailableError
from app.schemas.question import GenerateRequest
from app.services.question_generator import InvalidQuestionsError, QuestionGenerator


def output() -> dict:
    return {"question_text": "What is binary search?", "options": [
        {"text": "A search algorithm", "isCorrect": True},
        {"text": "A graph", "isCorrect": False},
        {"text": "A queue", "isCorrect": False},
        {"text": "A list", "isCorrect": False},
    ]}


def request(count: int = 1) -> GenerateRequest:
    return GenerateRequest(
        text="Binary search is a search algorithm for sorted input.",
        question_format="KNOWLEDGE", count=count,
    )


def test_capacity_limit_and_caller_cancellation_release_slot() -> None:
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingProvider:
            async def generate(self, prompt):
                started.set()
                await release.wait()
                return json.dumps({"questions": [output()]})

        generator = QuestionGenerator(BlockingProvider(), timeout_ms=5000, max_concurrency=1)
        active = asyncio.create_task(generator.generate(request()))
        await started.wait()
        try:
            with pytest.raises(ServiceUnavailableError):
                await generator.generate(request())
        finally:
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
        release.set()
        assert len((await generator.generate(request())).questions) == 1

    asyncio.run(run())


@pytest.mark.parametrize("case", ["duplicate", "boolean_string"])
def test_json_validation_keeps_semantic_and_strict_type_checks(case) -> None:
    class Provider:
        async def generate(self, prompt):
            question = output()
            if case == "boolean_string":
                question["options"][0]["isCorrect"] = "true"
            questions = [question, question] if case == "duplicate" else [question]
            return json.dumps({"questions": questions})

    with pytest.raises(InvalidQuestionsError):
        asyncio.run(QuestionGenerator(Provider()).generate(request(2 if case == "duplicate" else 1)))
