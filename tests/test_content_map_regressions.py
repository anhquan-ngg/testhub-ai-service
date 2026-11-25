"""Offline regressions for complete source reconstruction and outline isolation."""
import asyncio
import tracemalloc
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas.document import ExtractionResult
from app.schemas.rag import IndexCommand, PrepareIndexRequest, current_profile
from app.services.chunking import chunk_document
from app.services.content_map import content_map
from app.services.rag_errors import RagError
from tests.rag_http_fixture import Repository, enable_rag_fixture


def extraction(text):
    return ExtractionResult(
        contract_version="document-extraction.v1", source_type="PDF",
        source_sha256="a" * 64, parser_version="fixture", include_notes=False,
        status="EXTRACTED", unit_count=1,
        spans=[{"kind": "PAGE", "position": 1, "text": text}], warnings=[],
    )


def index_command():
    return IndexCommand(
        index_id=uuid4(), profile_id=current_profile().profile_id,
        scope={key: uuid4() for key in ("owner_id", "topic_id", "chapter_id", "document_id", "version_id")},
    )


def source_chunk(text, *, start=0, position=1):
    return {
        "document_id": "00000000-0000-4000-8000-000000000001",
        "version_id": "00000000-0000-4000-8000-000000000002", "source_sha256": "a" * 64,
        "kind": "PAGE", "position": position, "start_offset": start,
        "end_offset": start + len(text), "text": text,
    }


@pytest.mark.parametrize("gap", [" " * 5000, "\n\t" * 2500], ids=["spaces", "lines"])
@pytest.mark.parametrize("suffix", ["b" * 2500, ""], ids=["content", "trailing"])
def test_whitespace_gaps_never_emit_an_overlap_only_chunk(gap, suffix):
    text = "a" * 1200 + gap + suffix
    chunks = chunk_document(uuid4(), extraction(text))
    assert all(left.end_offset < right.end_offset for left, right in zip(chunks, chunks[1:]))
    assert all(chunk.text == text[chunk.start_offset:chunk.end_offset] for chunk in chunks)
    covered = {offset for chunk in chunks for offset in range(chunk.start_offset, chunk.end_offset)}
    assert all(offset in covered for offset, char in enumerate(text) if not char.isspace())
    if suffix:
        following = [chunk for chunk in chunks if chunk.start_offset >= 1200 + len(gap)]
        assert len(following) >= 2
        assert following[1].start_offset < following[0].end_offset
        overlap = text[following[1].start_offset:following[0].end_offset]
        assert overlap.strip() and len(overlap.encode()) <= 240
    else:
        assert len(chunks) == 1


@pytest.mark.parametrize("overlap", [False, True])
def test_reconstruction_accepts_exact_million_characters_and_counts_only_new_text(overlap):
    chunks = ([source_chunk("x" * 600_000), source_chunk("x" * 600_000, start=400_000)]
              if overlap else [source_chunk("x" * 400_000), source_chunk("y" * 600_000, position=2)])
    result = content_map(uuid4(), "Course", chunks)
    assert len(result.source_units) == (1 if overlap else 2)
    assert result.nodes


@pytest.mark.parametrize("growth", ["gap", "text", "later_unit"])
def test_reconstruction_rejects_excess_before_allocating_it(growth):
    chunks = {
        "gap": [source_chunk("x", start=2_000_000)],
        "text": [source_chunk("x" * 1_000_001)],
        "later_unit": [source_chunk("x" * 600_000), source_chunk("y" * 900_000, position=2)],
    }[growth]
    tracemalloc.start()
    try:
        with pytest.raises(RagError, match="RAG_OUTLINE_LIMIT"):
            content_map(uuid4(), "Course", chunks)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # A prior valid unit may exist, but an over-budget extension must not be allocated.
    assert peak < (8_000_000 if growth == "later_unit" else 2_000_000)


def test_every_heading_marks_all_ancestors_before_another_heading_replaces_active():
    texts = [
        "Part I: Alpha\n1. First local\n1.1. First detail\nExplanation.",
        "1.1.1. Deeper detail\nPart II: Beta\n1. Second local\nExplanation.",
        "Further explanation without a heading.",
    ]
    result = content_map(uuid4(), "Course", [source_chunk(text, position=i + 1) for i, text in enumerate(texts)])
    by_name = {node.name: node for node in result.nodes}
    for name in ("Alpha", "First local", "First detail", "Deeper detail"):
        assert 2 in {unit.position for unit in by_name[name].source_units}
        assert 3 not in {unit.position for unit in by_name[name].source_units}
    for name in ("Beta", "Second local"):
        assert [unit.position for unit in by_name[name].source_units] == [2, 3]


def test_local_numbered_sections_are_scoped_to_current_part_and_longest_prefix():
    texts = [
        "Part I: Alpha\n1. Local topic\n1.1. Detail topic\n1.1.1. Deep topic\nExplanation.",
        "Part II: Beta\n1. Local topic\n1.1. Detail topic\n1.1.1. Deep topic\nExplanation.",
        "1.1. Detail topic (2)\nContinued explanation.",
    ]
    result = content_map(uuid4(), "Course", [source_chunk(text, position=i + 1) for i, text in enumerate(texts)])
    roots = {node.name: node for node in result.nodes if node.parent_id is None}
    assert set(roots) == {"Alpha", "Beta"}
    assert len(result.nodes) == 8
    for root_name, positions in (("Alpha", [1]), ("Beta", [2, 3])):
        parent = roots[root_name]
        for name in ("Local topic", "Detail topic", "Deep topic"):
            child = next(node for node in result.nodes if node.parent_id == parent.id and node.name == name)
            expected = positions if name != "Deep topic" else positions[:1]
            assert [unit.position for unit in child.source_units] == expected
            parent = child


@pytest.mark.parametrize("failure", ["owner", "profile", "index", "BUILDING", "REVOKED"])
def test_fixture_outline_rejects_foreign_or_not_ready_scope(failure):
    async def run():
        repository = Repository()
        request = index_command()
        prepared = PrepareIndexRequest(**request.model_dump(), extraction=extraction("1. Topic\nExplanation."))
        chunks = chunk_document(request.index_id, prepared.extraction)
        await repository.prepare(prepared, current_profile(), chunks, "fixture")
        await repository.save(request, uuid4(), [])
        owner, profile, index = request.scope.owner_id, request.profile_id, request.index_id
        if failure == "owner":
            owner = uuid4()
        elif failure == "profile":
            profile = "other-profile"
        elif failure == "index":
            index = uuid4()
        else:
            repository.rows[index]["status"] = failure
        with pytest.raises(RagError, match="RAG_SCOPE_UNAVAILABLE"):
            await repository.outline_chunks(owner, profile, index)
    asyncio.run(run())


def test_fixture_outline_endpoint_returns_only_the_requested_ready_index():
    settings = Settings(internal_api_token="outline-fixture", rag_enabled=False, gemini_api_key="")
    headers = {"X-Internal-Token": settings.internal_api_token}
    with TestClient(enable_rag_fixture(create_app(settings))) as client:
        request = index_command()
        for title, index_id in (("Selected", request.index_id), ("Other", uuid4())):
            command = request.model_copy(update={"index_id": index_id})
            prepared = PrepareIndexRequest(**command.model_dump(), extraction=extraction(f"1. {title}\nExplanation."))
            assert client.post("/internal/rag/prepare", headers=headers, json=prepared.model_dump(mode="json")).status_code == 200
            assert client.post("/internal/rag/step", headers=headers, json=command.model_dump(mode="json")).status_code == 200
        response = client.post("/internal/rag/outline", headers=headers, json={
            "owner_id": str(request.scope.owner_id), "index_id": str(request.index_id),
            "profile_id": request.profile_id, "title": "Course",
        })
        assert response.status_code == 200, response.text
        result = response.json()
        assert [node["name"] for node in result["nodes"]] == ["Selected"]
        assert result["document_id"] == str(request.scope.document_id)
        assert result["version_id"] == str(request.scope.version_id)
        assert result["source_sha256"] == "a" * 64


def scope_of(result, name_part):
    node = next(n for n in result.nodes if name_part in n.name)
    return {u.position for u in node.source_units}

def pages_chunks(pages):
    return [source_chunk(text, position=position) for position, text in pages.items()]

LONG = "Nội dung chi tiết về dịch vụ web và cách thức triển khai trong thực tế. " * 4

def test_agenda_heading_expands_to_following_content_pages():
    chunks = pages_chunks({
        1: "3.2. Xây dựng Web API\n3.2.1. Công nghệ .Net Core\n3.2.2. Công nghệ NodeJS",
        2: "3.2.1. Xây dựng Web API với .NET Core\n" + LONG,
        3: LONG, 4: "3.2.2. Công nghệ NodeJS\n" + LONG})
    result = content_map(uuid4(), "Doc", chunks)
    assert {1, 2, 3} <= scope_of(result, "Công nghệ .Net Core")
    assert 4 not in scope_of(result, "Công nghệ .Net Core")
    assert "TOC_SCOPE_EXPANDED" in result.warnings

def test_title_only_slide_borrows_nearest_substantive_page():
    chunks = pages_chunks({1: "Tổng quan điểm yếu IoT\n" + LONG, 2: "1. Giao diện quản trị không an toàn", 3: "2. Mật khẩu yếu"})
    result = content_map(uuid4(), "Doc", chunks)
    assert 1 in scope_of(result, "Giao diện quản trị")
    assert 1 in scope_of(result, "Mật khẩu yếu")

def test_substantive_heading_is_not_expanded():
    chunks = pages_chunks({1: "1. Giới thiệu\n" + LONG, 2: "2. Kết luận\n" + LONG})
    result = content_map(uuid4(), "Doc", chunks)
    assert scope_of(result, "Giới thiệu") == {1}
    assert "TOC_SCOPE_EXPANDED" not in result.warnings
