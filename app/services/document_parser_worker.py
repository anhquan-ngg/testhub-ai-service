"""Disposable parser process. No network, DB, provider calls or source logging."""

from collections import Counter
import hashlib
from importlib.metadata import version
import io
import json
import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path

from app.schemas.document import (
    ExtractionQuality, ExtractionResult, MAX_FILE_BYTES, MAX_RESULT_BYTES, MAX_TEXT_CHARS,
    MAX_UNITS, SourceSpan,
)


# Header/footer-only text is not evidence of a usable source.
MIN_USEFUL_CHARS = 200
MIN_USEFUL_UNITS = 2
MAX_MARGIN_LINE_CHARS = 100


def _content_without_repeated_margins(spans: list[SourceSpan]) -> list[str]:
    """Discount common short boundary blocks, but retain repeated interior text."""
    units = [[" ".join(line.split()) for line in span.text.splitlines() if line.strip()]
             for span in spans]
    nonempty = [lines for lines in units if lines]
    occurrences = Counter(line for lines in nonempty for line in set(lines)
                          if len(line) <= MAX_MARGIN_LINE_CHARS)
    repeated = {line for line, count in occurrences.items()
                if count >= 2 and count * 2 >= len(nonempty)}
    content: list[str] = []
    for lines in units:
        # Keep source spans untouched so citations retain their original offsets.
        start, end = 0, len(lines)
        while start < end and lines[start] in repeated:
            start += 1
        while end > start and lines[end - 1] in repeated:
            end -= 1
        content.append("\n".join(lines[start:end]))
    return content


class DocumentRejected(Exception):
    """Only an allowlisted error code may cross the process boundary."""


ZIP_EXPANSION_LIMIT = 100 * 1024 * 1024
ZIP_BLOCK_BYTES = 64 * 1024


def _inflate_entry(data: bytes, entry: zipfile.ZipInfo, remaining: int) -> bytes:
    name_size, extra_size = struct.unpack_from("<HH", data, entry.header_offset + 26)
    cursor = entry.header_offset + 30 + name_size + extra_size
    end = cursor + entry.compress_size
    if end > len(data):
        raise DocumentRejected("DOCUMENT_INVALID")
    content = bytearray()

    def append(block: bytes) -> None:
        if len(content) + len(block) > remaining:
            raise DocumentRejected("DOCUMENT_LIMIT_EXCEEDED")
        content.extend(block)

    if entry.compress_type == zipfile.ZIP_STORED:
        while cursor < end:
            boundary = min(end, cursor + ZIP_BLOCK_BYTES)
            append(data[cursor:boundary])
            cursor = boundary
    else:
        inflater = zlib.decompressobj(-zlib.MAX_WBITS)
        pending = b""
        while True:
            if not pending and cursor < end:
                boundary = min(end, cursor + ZIP_BLOCK_BYTES)
                pending = data[cursor:boundary]
                cursor = boundary
            block = inflater.decompress(pending, min(ZIP_BLOCK_BYTES, remaining - len(content) + 1))
            pending = inflater.unconsumed_tail
            append(block)
            if inflater.eof:
                if pending or inflater.unused_data or cursor != end:
                    raise DocumentRejected("DOCUMENT_INVALID")
                break
            if not block and not pending and cursor == end:
                raise DocumentRejected("DOCUMENT_INVALID")
    if len(content) != entry.file_size or zlib.crc32(content) != entry.CRC:
        raise DocumentRejected("DOCUMENT_INVALID")
    return bytes(content)


def checked_pptx(data: bytes) -> io.BytesIO:
    """Repack only bytes actually inflated within a cumulative expansion limit."""
    target = io.BytesIO()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(target, "w") as dest:
            entries = source.infolist()
            if len(entries) > 5000:
                raise DocumentRejected("DOCUMENT_LIMIT_EXCEEDED")
            names: set[str] = set()
            total = 0
            for entry in entries:
                name = entry.filename
                if (name in names or name.startswith("/") or "\\" in name
                    or any(part in (".", "..") for part in name.split("/"))
                    or entry.flag_bits & 1 or entry.compress_type not in (0, 8)):
                    raise DocumentRejected("DOCUMENT_INVALID")
                names.add(name)
                remaining = ZIP_EXPANSION_LIMIT - total
                if entry.file_size > remaining:
                    raise DocumentRejected("DOCUMENT_LIMIT_EXCEEDED")
                # Validate local headers and overlaps, but do not let ZipExtFile's
                # declared-size clamp hide extra output or a truncated deflate stream.
                with source.open(entry):
                    content = _inflate_entry(data, entry, remaining)
                total += len(content)
                dest.writestr(name, content)
            if not {"[Content_Types].xml", "ppt/presentation.xml"}.issubset(names):
                raise DocumentRejected("DOCUMENT_INVALID")
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError, struct.error, zlib.error):
        raise DocumentRejected("DOCUMENT_INVALID") from None
    target.seek(0)
    return target


def extract(path: Path, source_type: str, include_notes: bool) -> ExtractionResult:
    """Preserve page/slide identities without rendering or executing embedded data."""
    data = path.read_bytes()
    if not data or len(data) > MAX_FILE_BYTES:
        raise DocumentRejected("DOCUMENT_LIMIT_EXCEEDED")
    spans: list[SourceSpan] = []
    image_units: set[int] = set()
    chars = 0

    def add(kind: str, position: int, text: str) -> None:
        nonlocal chars
        text = text.replace("\x00", "").strip()
        chars += len(text)
        if chars > MAX_TEXT_CHARS:
            raise DocumentRejected("DOCUMENT_LIMIT_EXCEEDED")
        spans.append(SourceSpan(kind=kind, position=position, text=text))

    if source_type == "PDF":
        from pypdf import PdfReader
        if not data.startswith(b"%PDF-"):
            raise DocumentRejected("DOCUMENT_INVALID")
        # Reject structural violations reported by pypdf's strict mode; do not
        # silently repair malformed PDFs even when strict=False could recover them.
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise DocumentRejected("DOCUMENT_ENCRYPTED")
        count = len(reader.pages)
        if not 1 <= count <= MAX_UNITS:
            raise DocumentRejected("DOCUMENT_LIMIT_EXCEEDED")
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            add("PAGE", number, text)
            if page.images:
                image_units.add(number)
    elif source_type == "PPTX":
        from pptx import Presentation
        from pptx.opc.constants import RELATIONSHIP_TYPE as RT
        from pptx.oxml import parse_xml
        from pptx.shapes.picture import Picture
        from pptx.slide import Slide
        presentation = Presentation(checked_pptx(data))
        count = len(presentation.slides)
        if not 1 <= count <= MAX_UNITS:
            raise DocumentRejected("DOCUMENT_LIMIT_EXCEEDED")

        def shape_text(shapes: object) -> list[str]:
            parts: list[str] = []
            for shape in shapes:
                if shape.shape_type == 6:  # GROUP
                    parts.extend(shape_text(shape.shapes))
                elif shape.has_table:
                    parts.extend("\t".join(cell.text for cell in row.cells) for row in shape.table.rows)
                elif shape.has_text_frame:
                    parts.append(shape.text_frame.text)
            return parts

        def contains_image(shapes: object) -> bool:
            return any(isinstance(shape, Picture) or
                       (shape.shape_type == 6 and contains_image(shape.shapes))
                       for shape in shapes)

        def background_has_image(slide: Slide) -> bool:
            for owner in (slide, slide.slide_layout, slide.slide_layout.slide_master):
                background = owner._element.cSld.bg
                if background is None:
                    continue
                if background.bgPr is not None:
                    return bool(background.bgPr.xpath(".//a:blip"))
                reference = background.bgRef
                if reference is None:
                    return False
                index = int(reference.get("idx", "0"))
                if index in (0, 1000):
                    return False
                # Style references are one-based; background styles start at 1001.
                theme = slide.slide_layout.slide_master.part.part_related_by(RT.THEME)
                root = parse_xml(theme.blob)
                styles = root.xpath("./a:themeElements/a:fmtScheme/" + (
                    "a:bgFillStyleLst/*" if index >= 1001 else "a:fillStyleLst/*"
                ))
                offset = index - 1001 if index >= 1001 else index - 1
                return 0 <= offset < len(styles) and bool(styles[offset].xpath(".//a:blip"))
            return False

        for number, slide in enumerate(presentation.slides, 1):
            text = "\n".join(shape_text(slide.shapes))
            add("SLIDE", number, text)
            if contains_image(slide.shapes) or background_has_image(slide):
                image_units.add(number)
            if include_notes and slide.has_notes_slide:
                frame = slide.notes_slide.notes_text_frame
                if frame is not None and frame.text.strip():
                    add("NOTES", number, frame.text)
    else:
        raise DocumentRejected("DOCUMENT_INVALID")
    base = [span for span in spans if span.kind != "NOTES"]
    text_units = [span.position for span in base if span.text]
    content = _content_without_repeated_margins(base)
    ocr_suspected = [span.position for span, body in zip(base, content)
                     if span.position in image_units and not body.strip()]
    blank = [span.position for span in base if not span.text and span.position not in ocr_suspected]
    useful_chars = sum(len("".join(text.split())) for text in content)
    required_units = min(MIN_USEFUL_UNITS, max(1, count - 1))
    useful = sum(bool(text) for text in content) >= required_units and useful_chars >= MIN_USEFUL_CHARS
    if not ocr_suspected:
        # No unit looks scanned. Text-less sources stay blocked, but are not OCR candidates.
        has_content = any(content) or any(span.text for span in spans if span.kind == "NOTES")
        status = "EXTRACTED" if has_content else "OCR_REQUIRED"
        warnings = ["NO_USABLE_TEXT"] if status == "OCR_REQUIRED" else []
    elif useful:
        status, warnings = "PARTIAL", ["PARTIAL_EXTRACTION"]
    else:
        status, warnings = "OCR_REQUIRED", ["OCR_REQUIRED"]
    if blank or ocr_suspected:
        warnings = ["EMPTY_UNITS"] + warnings
    return ExtractionResult(
        source_type=source_type, source_sha256=hashlib.sha256(data).hexdigest(),
        parser_version=f"{source_type.lower()}/{version('pypdf' if source_type == 'PDF' else 'python-pptx')};text/v3",
        include_notes=include_notes, unit_count=count, spans=spans, status=status, warnings=warnings,
        quality=ExtractionQuality(total_units=count, text_units=len(text_units),
                                  ocr_suspected_units=ocr_suspected, blank_units=blank),
    )

def main() -> None:
    """Apply POSIX memory/CPU caps before importing third-party parsers."""
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT_BYTES,) * 2)
    output = Path(sys.argv[2])
    try:
        result = extract(Path(sys.argv[1]), sys.argv[3], sys.argv[4] == "true")
        encoded = result.model_dump_json().encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            raise DocumentRejected("DOCUMENT_LIMIT_EXCEEDED")
    except DocumentRejected as exc:
        encoded = json.dumps({"error": str(exc)}).encode()
    except MemoryError:
        encoded = b'{"error":"DOCUMENT_LIMIT_EXCEEDED"}'
    except Exception:
        encoded = b'{"error":"DOCUMENT_INVALID"}'
    output.write_bytes(encoded)


if __name__ == "__main__":
    main()
