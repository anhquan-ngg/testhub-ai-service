"""Offline regressions for parser ownership, manifests and hostile containers."""

import asyncio
import io
from pathlib import Path
import struct
import threading
import zipfile

import pytest
from PIL import Image
from pptx import Presentation
from pptx.oxml.xmlchemy import OxmlElement
from pydantic import ValidationError

from app.schemas.document import ExtractionResult
from app.services.document_parser import DocumentParser, ExtractionError
from app.services.document_parser_worker import DocumentRejected, checked_pptx, extract
from tests.test_documents import mixed_pdf, pdf_bytes


def partial_result(quality):
    return ExtractionResult(
        source_type="PDF", source_sha256="a" * 64, parser_version="test/v1",
        include_notes=False, status="PARTIAL", unit_count=3,
        spans=[{"kind": "PAGE", "position": n, "text": "Body" if n == 1 else ""}
               for n in range(1, 4)], warnings=[], quality=quality,
    )


def test_partial_quality_is_optional():
    assert partial_result(None).quality is None


@pytest.mark.parametrize("updates", [
    {"total_units": 2}, {"text_units": 4},
    {"ocr_suspected_units": [0]}, {"ocr_suspected_units": [4]},
    {"ocr_suspected_units": [2, 2]}, {"blank_units": [3, 3]},
    {"blank_units": [0]}, {"blank_units": [4]}, {"blank_units": [2]},
    {"ocr_suspected_units": []},
])
def test_partial_rejects_inconsistent_quality(updates):
    quality = dict(total_units=3, text_units=1, ocr_suspected_units=[2], blank_units=[3])
    quality.update(updates)
    with pytest.raises(ValidationError):
        partial_result(quality)


def test_partial_accepts_valid_disjoint_quality():
    assert partial_result(dict(total_units=3, text_units=1,
                               ocr_suspected_units=[2], blank_units=[3])).unit_count == 3


def pptx_with_background(*, inherited=False, text=""):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, "PNG")
    image.seek(0)
    owner = (slide.slide_layout.slide_master if inherited == "master"
             else slide.slide_layout if inherited else slide)
    _, relationship = owner.part.get_or_add_image_part(image)
    bg = owner._element.cSld.get_or_add_bg()
    fill = OxmlElement("a:blipFill")
    blip = OxmlElement("a:blip")
    from pptx.oxml.ns import qn
    blip.set(qn("r:embed"), relationship)
    fill.append(blip)
    bg.append(fill)
    if text:
        slide.shapes.add_textbox(0, 0, 1000000, 1000000).text = text
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


@pytest.mark.parametrize("inherited", [False, True, "master"])
@pytest.mark.parametrize("text", ["", "A useful short slide"])
def test_pptx_background_images_are_ocr_candidates(tmp_path, inherited, text):
    source = tmp_path / "background.pptx"
    source.write_bytes(pptx_with_background(inherited=inherited, text=text))
    result = extract(source, "PPTX", False)
    assert result.status == ("EXTRACTED" if text else "OCR_REQUIRED")
    assert result.quality.ocr_suspected_units == ([] if text else [1])
    assert result.quality.blank_units == []


def test_blank_pptx_stays_blank(tmp_path):
    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6])
    source = tmp_path / "blank.pptx"
    presentation.save(source)
    result = extract(source, "PPTX", False)
    assert result.quality.blank_units == [1]
    assert result.quality.ocr_suspected_units == []


def zip_bytes(content, compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", content)
        archive.writestr("ppt/presentation.xml", b"presentation")
    return output.getvalue()


def test_pptx_rejects_forged_uncompressed_size():
    data = bytearray(zip_bytes(b"x" * 200000))
    central = data.index(b"PK\x01\x02")
    # A central size of one makes ZipExtFile truncate output and check this CRC.
    import zlib
    struct.pack_into("I", data, central + 16, zlib.crc32(b"x"))
    struct.pack_into("I", data, central + 24, 1)
    struct.pack_into("I", data, 14, zlib.crc32(b"x"))
    struct.pack_into("I", data, 22, 1)
    with pytest.raises(DocumentRejected, match="DOCUMENT_INVALID"):
        checked_pptx(bytes(data))


def test_pptx_rejects_truncated_deflate_stream():
    data = bytearray(zip_bytes(b"x" * 200000))
    central = data.index(b"PK\x01\x02")
    compressed = struct.unpack_from("I", data, central + 20)[0]
    struct.pack_into("I", data, central + 20, compressed - 1)
    struct.pack_into("I", data, 18, compressed - 1)
    with pytest.raises(DocumentRejected, match="DOCUMENT_INVALID"):
        checked_pptx(bytes(data))


def test_pptx_repack_preserves_multi_block_content():
    content = bytes(range(256)) * 1024
    with zipfile.ZipFile(checked_pptx(zip_bytes(content))) as archive:
        assert archive.read("[Content_Types].xml") == content


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_pptx_cumulative_expansion_boundary(monkeypatch, compression):
    from app.services import document_parser_worker as worker
    data = zip_bytes(b"x" * 150000, compression)
    monkeypatch.setattr(worker, "ZIP_EXPANSION_LIMIT", 150000 + len(b"presentation"))
    checked_pptx(data)
    monkeypatch.setattr(worker, "ZIP_EXPANSION_LIMIT", 150000 + len(b"presentation") - 1)
    with pytest.raises(DocumentRejected, match="DOCUMENT_LIMIT_EXCEEDED"):
        checked_pptx(data)


def test_pptx_forged_size_cannot_bypass_actual_expansion_limit(monkeypatch):
    from app.services import document_parser_worker as worker
    data = bytearray(zip_bytes(b"x" * 200000))
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, central + 24, 1)
    struct.pack_into("<I", data, 22, 1)
    monkeypatch.setattr(worker, "ZIP_EXPANSION_LIMIT", 100000)
    with pytest.raises(DocumentRejected, match="DOCUMENT_LIMIT_EXCEEDED"):
        checked_pptx(bytes(data))


def test_pptx_decompression_uses_bounded_input_and_output_blocks(monkeypatch):
    import random
    from app.services import document_parser_worker as worker
    content = random.Random(0).randbytes(200000)
    data = zip_bytes(content)
    original = worker.zlib.decompressobj
    calls = []

    class BoundedInflater:
        def __init__(self, *args):
            self.inner = original(*args)

        def decompress(self, block, max_length):
            assert len(block) <= worker.ZIP_BLOCK_BYTES
            assert 0 < max_length <= worker.ZIP_BLOCK_BYTES
            calls.append(max_length)
            return self.inner.decompress(block, max_length)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(worker.zlib, "decompressobj", BoundedInflater)
    with zipfile.ZipFile(checked_pptx(data)) as archive:
        assert archive.read("[Content_Types].xml") == content
    assert len(calls) >= 4


def test_solid_slide_background_overrides_inherited_image(tmp_path):
    presentation = Presentation(io.BytesIO(pptx_with_background(inherited=True)))
    presentation.slides[0].background.fill.solid()
    source = tmp_path / "override.pptx"
    presentation.save(source)
    result = extract(source, "PPTX", False)
    assert result.quality.ocr_suspected_units == []
    assert result.quality.blank_units == [1]


@pytest.mark.parametrize("useful_body", [True, False])
def test_image_page_with_repeated_header_needs_ocr(tmp_path, useful_body):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    body = "Normalization removes redundancy in database relations. " * 5 if useful_body else ""
    reader = PdfReader(io.BytesIO(mixed_pdf(["Course header\n" + body] * 2)))
    writer = PdfWriter()
    for number, page in enumerate(reader.pages):
        writer.add_page(page)
        if number == 0:
            stream = DecodedStreamObject()
            stream.set_data(b"q 100 0 0 100 0 0 cm /Im0 Do Q BT /F1 12 Tf 0 0 Td (Course header) Tj ET")
            # Reuse the same font resources as the following textual page.
            page = writer.pages[-1]
            page[NameObject("/Resources")][NameObject("/Font")] = reader.pages[1]["/Resources"]["/Font"]
            page[NameObject("/Contents")] = writer._add_object(stream)
    source = tmp_path / "scan-header.pdf"
    writer.write(source)
    result = extract(source, "PDF", False)
    assert result.status == ("PARTIAL" if useful_body else "OCR_REQUIRED")
    assert result.quality.ocr_suspected_units == [1]
    assert result.quality.blank_units == []
    assert result.spans[0].text == "Course header"
    assert result.spans[1].text.startswith("Course header")


def test_short_document_with_one_substantial_page_can_be_partial(tmp_path):
    source = tmp_path / "short.pdf"
    source.write_bytes(mixed_pdf(["Normalization removes redundancy in relations. " * 6]))
    result = extract(source, "PDF", False)
    assert result.status == "PARTIAL"
    assert result.quality.ocr_suspected_units == [1]


class DelayedChild:
    def __init__(self):
        self.killed = False
        self.exited = threading.Event()
        self.waiting = threading.Event()

    def poll(self):
        return -9 if self.exited.is_set() else None

    def kill(self):
        self.killed = True

    def wait(self):
        self.waiting.set()
        assert self.exited.wait(5)
        return -9


@pytest.mark.parametrize("cancel_close", [False, True])
def test_shutdown_owns_delayed_spawn_and_reap(monkeypatch, cancel_close):
    from app.services import document_parser
    spawn_started, spawn_release = threading.Event(), threading.Event()
    child = DelayedChild()
    folders = []

    async def unsupported(*args, **kwargs):
        raise NotImplementedError

    def spawn(args, **kwargs):
        folders.append(Path(args[4]).parent)
        spawn_started.set()
        assert spawn_release.wait(5)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unsupported)
    monkeypatch.setattr(document_parser.subprocess, "Popen", spawn)

    async def stream():
        yield pdf_bytes()

    async def run():
        parser = DocumentParser(max_concurrency=1)
        active = asyncio.create_task(parser.extract(stream(), "PDF", False))
        closing = None
        try:
            assert await asyncio.to_thread(spawn_started.wait, 2)
            # Shutdown starts BEFORE request cancellation registers a cleanup task.
            closing = asyncio.create_task(parser.aclose())
            await asyncio.sleep(0.02)
            assert not closing.done()
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            assert parser._cleanup_tasks
            if cancel_close:
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
                assert all(not task.cancelled() for task in parser._cleanup_tasks)
            spawn_release.set()
            assert await asyncio.to_thread(child.waiting.wait, 2)
            assert child.killed and parser._slots._value == 0
            assert folders[0].exists()
            assert all(not task.done() for task in parser._cleanup_tasks)
            if cancel_close:
                for task in parser._cleanup_tasks:
                    task.cancel()
                await asyncio.sleep(0.02)
                assert parser._slots._value == 0 and folders[0].exists()
                assert all(not task.done() for task in parser._cleanup_tasks)
            if not cancel_close:
                assert not closing.done()
            child.exited.set()
            await asyncio.wait_for(parser.aclose(), 2)
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            assert parser._slots._value == 1 and not parser._cleanup_tasks
            assert not folders[0].exists()
            with pytest.raises(ExtractionError, match="DOCUMENT_CAPACITY_REACHED"):
                await parser.extract(stream(), "PDF", False)
        finally:
            spawn_release.set()
            child.exited.set()
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            await parser.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("threaded", [False, True])
def test_repeated_request_cancellation_retains_wait_ownership(monkeypatch, threaded):
    from app.services import document_parser
    child = DelayedChild()
    folders = []

    class NativeChild:
        @property
        def returncode(self):
            return child.poll()

        def kill(self):
            child.kill()

        async def wait(self):
            return await asyncio.to_thread(child.wait)

    async def create(*args, **kwargs):
        if threaded:
            raise NotImplementedError
        folders.append(Path(args[4]).parent)
        return NativeChild()

    def spawn(args, **kwargs):
        folders.append(Path(args[4]).parent)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(document_parser.subprocess, "Popen", spawn)

    async def stream():
        yield pdf_bytes()

    async def run():
        parser = DocumentParser(max_concurrency=1)
        active = asyncio.create_task(parser.extract(stream(), "PDF", False))
        try:
            assert await asyncio.to_thread(child.waiting.wait, 2)
            active.cancel()
            for _ in range(100):
                if child.killed:
                    break
                await asyncio.sleep(0.005)
            assert child.killed
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            closing = asyncio.create_task(parser.aclose())
            await asyncio.sleep(0.02)
            assert not closing.done()
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
            assert parser._slots._value == 0 and folders[0].exists()
            assert all(not task.done() for task in parser._cleanup_tasks)
            child.exited.set()
            await asyncio.wait_for(parser.aclose(), 2)
            assert parser._slots._value == 1 and not folders[0].exists()
        finally:
            child.exited.set()
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            await parser.aclose()

    asyncio.run(run())
