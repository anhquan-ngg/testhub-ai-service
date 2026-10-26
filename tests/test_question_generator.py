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


def choice(text: str, correct: set[int]) -> dict:
    return {"question_text": text, "options": [
        {"text": option, "isCorrect": index in correct}
        for index, option in enumerate(["Alpha", "Beta", "Gamma", "Delta"])
    ]}


def mixed_request() -> GenerateRequest:
    return GenerateRequest(
        text="Binary search is a search algorithm for sorted input.",
        question_configs=[
            {"question_type": "SINGLE_CHOICE", "question_format": "KNOWLEDGE", "count": 1},
            {"question_type": "MULTIPLE_CHOICE", "question_format": "APPLYING", "count": 2},
        ],
    )


def test_question_configs_assign_type_and_level_by_position() -> None:
    prompts = []

    class Provider:
        async def generate(self, prompt):
            prompts.append(prompt)
            return json.dumps({"questions": [
                choice("Q1", {0}), choice("Q2", {0, 1}), choice("Q3", {1, 2, 3}),
            ]})

    result = asyncio.run(QuestionGenerator(Provider()).generate(mixed_request()))
    assert [(q.question_type.value, q.question_format.value) for q in result.questions] == [
        ("SINGLE_CHOICE", "KNOWLEDGE"), ("MULTIPLE_CHOICE", "APPLYING"), ("MULTIPLE_CHOICE", "APPLYING"),
    ]
    assert '"index": 3, "question_type": "MULTIPLE_CHOICE"' in prompts[0]
    assert result.model_dump(mode="json")["questions"][1]["question_type"] == "MULTIPLE_CHOICE"


@pytest.mark.parametrize("answers", [
    [{0}, {0}, {0, 1}],  # a MULTIPLE_CHOICE slot with one correct option
    [{0, 1}, {0, 1}, {0, 1}],  # a SINGLE_CHOICE slot with two correct options
    [{0}, {0, 1, 2, 3}, {0, 1}],  # every option correct
])
def test_question_configs_reject_answers_for_the_wrong_slot_type(answers) -> None:
    class Provider:
        async def generate(self, prompt):
            return json.dumps({"questions": [
                choice(f"Q{index}", correct) for index, correct in enumerate(answers)
            ]})

    with pytest.raises(InvalidQuestionsError):
        asyncio.run(QuestionGenerator(Provider()).generate(mixed_request()))


@pytest.mark.parametrize("change", [
    {"question_configs": [{"question_type": "ESSAY", "question_format": "KNOWLEDGE", "count": 1}]},
    {"question_configs": [{"question_type": "SINGLE_CHOICE", "question_format": "KNOWLEDGE", "count": 11}] * 2},
    {"question_configs": [{"question_type": "SINGLE_CHOICE", "question_format": "KNOWLEDGE", "count": 1}] * 21},
    {"question_configs": []},
    {"question_configs": [{"question_type": "SINGLE_CHOICE", "question_format": "KNOWLEDGE", "count": 1}],
     "count": 1},
    {"question_format": "KNOWLEDGE"},
])
def test_blueprint_rejects_unsupported_or_ambiguous_configs(change) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GenerateRequest.model_validate({"text": "Binary search works on sorted input.", **change})


def test_legacy_format_and_count_become_single_choice_slots() -> None:
    assert [(slot.question_type.value, slot.question_format.value) for slot in request(2).slots()] == [
        ("SINGLE_CHOICE", "KNOWLEDGE"), ("SINGLE_CHOICE", "KNOWLEDGE"),
    ]


@pytest.mark.parametrize("blueprint", [request, mixed_request])
def test_blueprint_dump_can_be_validated_again(blueprint) -> None:
    original = blueprint()
    slots = original.slots()
    for mode in ("python", "json"):
        restored = GenerateRequest.model_validate(original.model_dump(mode=mode))
        assert restored.slots() == slots
        assert restored.model_dump() == original.model_dump()
    if original.question_format is not None:
        assert original.question_configs is None


def test_readme_request_and_response_match_slots() -> None:
    import re
    from pathlib import Path
    from app.schemas.question import GenerateResponse
    from app.services.question_generator import check_slots

    section = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8").split(
        "## Contract `POST /internal/generate`", 1,
    )[1]
    examples = re.findall(r"```json\s*([\s\S]*?)```", section)
    command = GenerateRequest.model_validate_json(examples[0])
    response = GenerateResponse.model_validate_json(examples[1])
    check_slots(response.questions, command.slots())
