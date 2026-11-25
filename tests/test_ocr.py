import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from pptx import Presentation

from app.config import Settings
from app.repositories.ocr_cache import RepositoryOcrCache
from app.schemas.document import ExtractionQuality, ExtractionResult, SourceSpan
from app.services import document_parser_worker as worker
from app.services.document_parser import DocumentParser
from app.services.ocr import NO_TEXT, OcrClient, OcrDailyBudget, OcrError, OcrService
from tests.test_documents import mixed_pdf

PNG = b"\x89PNG\r\n\x1a\n" + b"image-a"
PNG_B = b"\x89PNG\r\n\x1a\n" + b"image-b"
BODY = "Normalization removes redundancy by decomposing relations. " * 4


def settings(**kwargs):
    values = {"internal_api_token": "t" * 32, "ocr_enabled": True, "ocr_model": "vision-x",
              "router_base_url": "http://router.test/v1", "router_api_key": "k", "ocr_max_attempts": 2}
    values.update(kwargs)
    return Settings(**values)


def completion(content, finish="stop", status=200, content_type="application/json"):
    body = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": finish}]})
    return httpx.Response(status, content=body, headers={"content-type": content_type})


def client_with(handler, **kwargs):
    return OcrClient(settings(**kwargs), transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)

async def stream(data):
    yield data


def test_client_sends_image_and_returns_transcript():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return completion("  Dòng 1\nDòng 2  ")

    async def go():
        client = client_with(handler)
        try:
            return await client.transcribe(PNG)
        finally:
            await client.aclose()

    assert run(go()) == "Dòng 1\nDòng 2"
    parts = seen["messages"][0]["content"]
    assert seen["model"] == "vision-x" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_client_retries_then_succeeds_and_exhausts(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr("app.services.ocr.asyncio.sleep", no_sleep)
    calls = []

    def flaky(request):
        calls.append(1)
        return completion("", status=429) if len(calls) == 1 else completion("ok")

    def down(request):
        calls.append(1)
        return completion("", status=503)

    async def go(handler):
        client = client_with(handler)
        try:
            return await client.transcribe(PNG)
        finally:
            await client.aclose()

    assert run(go(flaky)) == "ok" and len(calls) == 2
    calls.clear()
    with pytest.raises(OcrError):
        run(go(down))
    assert len(calls) == 2


@pytest.mark.parametrize("response,expected", [
    (completion(NO_TEXT), ""),
    (completion("x", finish="length"), OcrError),
    (completion("x", content_type="text/html"), OcrError),
    (completion("x", status=400), OcrError),
])
def test_client_rejects_untrusted_responses(response, expected):
    async def go():
        client = client_with(lambda request: response)
        try:
            return await client.transcribe(PNG)
        finally:
            await client.aclose()

    if expected is OcrError:
        with pytest.raises(OcrError):
            run(go())
    else:
        assert run(go()) == expected


def test_client_rejects_non_image():
    async def go():
        client = client_with(lambda request: completion("x"))
        try:
            await client.transcribe(b"GIF89a")
        finally:
            await client.aclose()

    with pytest.raises(OcrError):
        run(go())


def test_daily_budget_resets_each_utc_day():
    from datetime import datetime, timezone
    now = [datetime(2026, 1, 1, 23, tzinfo=timezone.utc)]
    budget = OcrDailyBudget(2, clock=lambda: now[0])
    assert [budget.try_acquire() for _ in range(3)] == [True, True, False]
    now[0] = datetime(2026, 1, 2, 1, tzinfo=timezone.utc)
    assert budget.try_acquire()
    assert OcrDailyBudget(0).try_acquire()


class FakeClient:
    def __init__(self, answers=None, fail=()):
        self.answers = answers or {}
        self.fail = set(fail)
        self.calls = []

    async def transcribe(self, image):
        self.calls.append(image)
        if image in self.fail:
            raise OcrError("boom")
        return self.answers.get(image, "Nội dung từ ảnh")


class FakeCache:
    def __init__(self, stored=None, fail=False):
        self.stored = dict(stored or {})
        self.fail = fail

    async def get_many(self, keys):
        if self.fail:
            raise RuntimeError("db down")
        return {key: self.stored[key] for key in keys if key in self.stored}

    async def put_many(self, items):
        if self.fail:
            raise RuntimeError("db down")
        self.stored.update(items)


def pdf_result(suspected=(1,), pages=4):
    spans = [SourceSpan(kind="PAGE", position=n, text="" if n in suspected else BODY) for n in range(1, pages + 1)]
    return ExtractionResult(
        source_type="PDF", source_sha256="a" * 64, parser_version="pdf/1;text/v4", include_notes=False,
        unit_count=pages, spans=spans, status="PARTIAL", warnings=["EMPTY_UNITS", "PARTIAL_EXTRACTION"],
        quality=ExtractionQuality(total_units=pages, text_units=pages - len(suspected),
                                  ocr_suspected_units=list(suspected), blank_units=[]))


def write_manifest(directory: Path, pages: dict[int, list[bytes]]):
    entries = []
    for position, blobs in pages.items():
        names = []
        for index, blob in enumerate(blobs):
            name = f"ocr-{position}-{index}.img"
            (directory / name).write_bytes(blob)
            names.append(name)
        entries.append({"position": position, "images": names})
    (directory / worker.OCR_MANIFEST).write_text(json.dumps({"pages": entries}), encoding="utf-8")


def service(client, cache=None, **kwargs):
    budget = kwargs.pop("budget", None)
    return OcrService(settings(**kwargs), client, cache, budget=budget)


def test_enrich_recovers_suspected_page_and_marks_provenance(tmp_path):
    write_manifest(tmp_path, {1: [PNG]})
    cache = FakeCache()
    fake = FakeClient({PNG: "Tiêu đề\nBảng | A | B"})
    result = run(service(fake, cache).enrich(pdf_result(), tmp_path, 20))
    assert result.status == "EXTRACTED" and result.warnings == []
    assert result.spans[0].text == "Tiêu đề\nBảng | A | B"
    assert result.spans[1].text == BODY
    assert result.quality.ocr_units == [1] and result.quality.ocr_suspected_units == []
    assert result.quality.text_units == 4 and len(cache.stored) == 1


def test_enrich_uses_cache_without_calling_model(tmp_path):
    write_manifest(tmp_path, {1: [PNG]})
    first = FakeClient({PNG: "Văn bản A"})
    cache = FakeCache()
    run(service(first, cache).enrich(pdf_result(), tmp_path, 20))
    second = FakeClient()
    result = run(service(second, cache).enrich(pdf_result(), tmp_path, 20))
    assert second.calls == [] and result.spans[0].text == "Văn bản A"


def test_cache_key_depends_on_model_version_and_bytes(tmp_path):
    a = service(FakeClient())
    assert a._key(PNG) != a._key(PNG_B)
    assert a._key(PNG) != service(FakeClient(), ocr_model="other")._key(PNG)
    assert a._key(PNG) != service(FakeClient(), ocr_config_version="v2")._key(PNG)


def test_enrich_dedupes_lines_already_in_text_layer(tmp_path):
    write_manifest(tmp_path, {2: [PNG]})
    base = pdf_result(suspected=(1,))
    base.spans[1].text = "Chương 1\n" + BODY
    result = run(service(FakeClient({PNG: "Chương 1\nChi tiết mới"})).enrich(base, tmp_path, 20))
    assert result.spans[1].text.endswith("\n\nChi tiết mới")
    assert result.spans[1].text.count("Chương 1") == 1
    assert 2 in result.quality.ocr_units


def test_failed_or_partial_pages_stay_unrecovered(tmp_path):
    write_manifest(tmp_path, {1: [PNG, PNG_B]})
    fake = FakeClient({PNG: "ok"}, fail=[PNG_B])
    original = pdf_result()
    result = run(service(fake).enrich(original, tmp_path, 20))
    assert result.spans == original.spans and result.quality.ocr_units == []
    assert [(p.position, p.status, p.reason) for p in result.quality.ocr_pages] == [(1, "FAILED", "PROVIDER_ERROR")]
    assert original.quality.ocr_pages == []

def test_respects_page_cap_daily_budget_and_time_budget(tmp_path):
    write_manifest(tmp_path, {1: [PNG], 2: [PNG_B]})
    base = pdf_result(suspected=(1, 2), pages=5)
    base.spans[3].text = base.spans[4].text = BODY
    fake = FakeClient()
    run(service(fake, ocr_max_pages=1).enrich(base, tmp_path, 20))
    assert len(fake.calls) == 1

    fake = FakeClient()
    limited = run(service(fake, budget=OcrDailyBudget(1)).enrich(base, tmp_path, 20))
    assert len(fake.calls) == 1
    assert limited.quality.ocr_pages[1].status == "BUDGET_EXHAUSTED"
    assert limited.quality.ocr_pages[1].reason == "DAILY_LIMIT"

    fake = FakeClient()
    timed = run(service(fake).enrich(base, tmp_path, 1))
    assert all(p.status == "BUDGET_EXHAUSTED" and p.reason == "TIME_BUDGET" for p in timed.quality.ocr_pages)
    assert timed.spans == base.spans and fake.calls == []


def test_fail_open_on_bad_manifest_cache_error_and_traversal(tmp_path):
    original = pdf_result()
    (tmp_path / worker.OCR_MANIFEST).write_text("{not json", encoding="utf-8")
    assert run(service(FakeClient()).enrich(original, tmp_path, 20)) is original

    outside = tmp_path / "outside.img"
    outside.write_bytes(PNG)
    inner = tmp_path / "ocr"
    inner.mkdir()
    (inner / worker.OCR_MANIFEST).write_text(
        json.dumps({"pages": [{"position": 1, "images": ["../outside.img"]}]}), encoding="utf-8")
    fake = FakeClient()
    assert run(service(fake).enrich(original, inner, 20)) is original and fake.calls == []

    write_manifest(tmp_path, {1: [PNG]})
    fake = FakeClient({PNG: "Văn bản"})
    result = run(service(fake, FakeCache(fail=True)).enrich(original, tmp_path, 20))
    assert result.spans == original.spans and fake.calls == []
    assert result.quality.ocr_pages[0].reason == "CACHE_UNAVAILABLE"


def test_repository_cache_adapter_delegates():
    class Repo:
        async def ocr_cache_get_many(self, keys, ttl_days):
            return {keys[0]: f"ttl={ttl_days}"}

        async def ocr_cache_put_many(self, items):
            self.items = items

    repo = Repo()
    cache = RepositoryOcrCache(repo, 7)
    assert run(cache.get_many(["k"])) == {"k": "ttl=7"}
    run(cache.put_many({"k": "v"}))
    assert repo.items == {"k": "v"}


def test_worker_renders_only_thin_image_pages_within_cap(tmp_path):
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(mixed_pdf([BODY, BODY, BODY]))
    render = worker.OcrRender(tmp_path / "ocr", 5, 72, 1_000_000)
    result = worker.extract(pdf, "PDF", False, render)
    manifest = json.loads((tmp_path / "ocr" / worker.OCR_MANIFEST).read_text(encoding="utf-8"))
    assert [page["position"] for page in manifest["pages"]] == [1]
    assert all((tmp_path / "ocr" / name).is_file() for name in manifest["pages"][0]["images"])
    assert result.status == "PARTIAL" and result.quality.ocr_units == []

    before = {path.name for path in tmp_path.iterdir()}
    plain = worker.extract(pdf, "PDF", False)
    assert plain.model_dump() == result.model_dump()
    assert {path.name for path in tmp_path.iterdir()} == before


def test_worker_pptx_extracts_embedded_picture(tmp_path):
    from PIL import Image
    picture = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(picture, "PNG")
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_picture(io.BytesIO(picture.getvalue()), 0, 0)
    path = tmp_path / "in.pptx"
    presentation.save(path)
    render = worker.OcrRender(tmp_path / "ocr", 5, 72, 1_000_000)
    worker.extract(path, "PPTX", False, render)
    manifest = json.loads((tmp_path / "ocr" / worker.OCR_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["pages"][0]["position"] == 1 and len(manifest["pages"][0]["images"]) == 1


def test_parser_runs_ocr_end_to_end_and_leaves_no_files(tmp_path):
    seen = []

    class Recorder(FakeClient):
        async def transcribe(self, image):
            seen.append(image[:4])
            return "Nội dung bìa"

    parser = DocumentParser(
        max_concurrency=1, ocr=service(Recorder()), ocr_render=(5, 72, 1_000_000), ocr_budget_seconds=20)
    result = run(parser.extract(stream(mixed_pdf([BODY, BODY, BODY])), "PDF", False,
                                ocr_mode="targeted", ocr_positions=(1,)))
    assert seen == [b"\x89PNG"] and result.status == "EXTRACTED"
    assert result.spans[0].text == "Nội dung bìa" and result.quality.ocr_units == [1]

    plain = run(DocumentParser(max_concurrency=1).extract(stream(mixed_pdf([BODY, BODY, BODY])), "PDF", False))
    assert plain.status == "PARTIAL" and plain.quality.ocr_units == []


def test_explicit_off_never_calls_vision_but_exposes_candidates():
    fake = FakeClient()
    parser = DocumentParser(ocr=service(fake), ocr_render=(5, 72, 1_000_000), ocr_budget_seconds=20)
    result = run(parser.extract(stream(mixed_pdf([BODY] * 3)), "PDF", False))
    assert fake.calls == []
    assert result.quality.ocr_candidate_units == [1]
    assert result.quality.ocr_pages[0].status == "PENDING"


def test_targeted_worker_bypasses_global_cap_and_reports_remaining_pages(tmp_path):
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(mixed_pdf([BODY] * 3)))
    writer = PdfWriter()
    for _ in range(35):
        writer.add_page(reader.pages[0])
    for page in reader.pages[1:]:
        writer.add_page(page)
    path = tmp_path / "many.pdf"
    with path.open("wb") as output:
        writer.write(output)
    render = worker.OcrRender(tmp_path / "ocr", 1, 72, 1_000_000, (35, 34))
    result = worker.extract(path, "PDF", False, render)
    manifest = json.loads((render.directory / worker.OCR_MANIFEST).read_text(encoding="utf-8"))
    assert [page["position"] for page in manifest["pages"]] == [35]
    assert result.quality.ocr_candidate_units == list(range(1, 36))
    pages = {page.position: page for page in result.quality.ocr_pages}
    assert pages[1].status == "PENDING"
    assert (pages[34].status, pages[34].reason) == ("BUDGET_EXHAUSTED", "PAGE_LIMIT")


def test_render_failure_is_visible_without_discarding_text(tmp_path, monkeypatch):
    path = tmp_path / "in.pdf"
    path.write_bytes(mixed_pdf([BODY] * 3))
    monkeypatch.setattr(worker, "_render_pdf_pages", lambda *args: {})
    result = worker.extract(path, "PDF", False, worker.OcrRender(tmp_path / "ocr", 1, 72, 1_000_000, (1,)))
    assert result.spans[1].text.strip() == BODY.strip()
    assert result.quality.ocr_pages[0].reason == "RENDER_FAILED"


def test_no_text_and_duplicate_transcription_are_not_provider_failures(tmp_path):
    write_manifest(tmp_path, {2: [PNG]})
    original = pdf_result()
    duplicate = run(service(FakeClient({PNG: BODY})).enrich(original, tmp_path, 20))
    assert duplicate.quality.ocr_units == [] and duplicate.spans == original.spans
    assert duplicate.quality.ocr_pages[0].status == "COMPLETED"
    assert duplicate.quality.ocr_pages[0].reason == "NO_ADDITIONAL_TEXT"
    empty_client = FakeClient()
    empty_client.answers[PNG] = ""
    empty = run(service(empty_client).enrich(original, tmp_path, 20))
    assert empty.quality.ocr_pages[0].status == "NO_TEXT"


def test_targeted_disabled_and_invalid_positions_do_not_call_provider():
    from app.services.document_parser import ExtractionError
    parser = DocumentParser()
    data = mixed_pdf([BODY] * 3)
    result = run(parser.extract(stream(data), "PDF", False, ocr_mode="targeted", ocr_positions=(1,)))
    assert result.quality.ocr_pages[0].status == "DISABLED"
    for mode, positions in [("targeted", ()), ("off", (1,)), ("targeted", (1, 1)),
                            ("targeted", (5,)), ("targeted", tuple(range(1, 32)))]:
        with pytest.raises(ExtractionError):
            run(parser.extract(stream(data), "PDF", False, ocr_mode=mode, ocr_positions=positions))
