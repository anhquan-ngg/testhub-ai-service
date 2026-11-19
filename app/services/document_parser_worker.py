"""Disposable parser process. No network, DB, provider calls or source logging."""

from collections import Counter
import hashlib
from importlib.metadata import version
import io
import json
import os
import sys
import zipfile
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
    """Exclude short, common boundary lines only for partial-source eligibility."""
    units = [[" ".join(line.split()) for line in span.text.splitlines() if line.strip()]
             for span in spans]
    nonempty = [lines for lines in units if lines]
    boundaries = Counter(line for lines in nonempty for line in {lines[0], lines[-1]}
                         if len(line) <= MAX_MARGIN_LINE_CHARS)
    repeated = {line for line, count in boundaries.items()
                if count >= 2 and count * 2 >= len(nonempty)}
    content: list[str] = []
    for lines in units:
        # Keep source spans untouched so citations retain their original offsets.
        start = 1 if lines and lines[0] in repeated else 0
        end = len(lines) - 1 if lines and lines[-1] in repeated else len(lines)
        content.append("\n".join(lines[start:end]))
    return content


class DocumentRejected(Exception):
    """Only an allowlisted error code may cross the process boundary."""


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
                remaining = 100 * 1024 * 1024 - total
                if entry.file_size > remaining:
                    raise DocumentRejected("DOCUMENT_LIMIT_EXCEEDED")
                # A forged central-directory size is not the expansion boundary:
                # read at most remaining+1, verify actual length, then repack.
                with source.open(entry) as stream:
                    content = stream.read(remaining + 1)
                total += len(content)
                if total > 100 * 1024 * 1024:
                    raise DocumentRejected("DOCUMENT_LIMIT_EXCEEDED")
                dest.writestr(name, content)
            if not {"[Content_Types].xml", "ppt/presentation.xml"}.issubset(names):
                raise DocumentRejected("DOCUMENT_INVALID")
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError):
        raise DocumentRejected("DOCUMENT_INVALID") from None
    target.seek(0)
    return target


def extract(path: Path, source_type: str, include_notes: bool) -> ExtractionResult:
    """Preserve page/slide identities without rendering or executing embedded data."""
    data = path.read_bytes()
    if not data or len(data) > MAX_FILE_BYTES:
        raise DocumentRejected("DOCUMENT_LIMIT_EXCEEDED")
    spans: list[SourceSpan] = []
    ocr_suspected: list[int] = []
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
            # Conservatively flag image-only pages even in a mixed PDF.
            if not text.strip() and bool(page.images):
                ocr_suspected.append(number)
    elif source_type == "PPTX":
        from pptx import Presentation
        from pptx.shapes.picture import Picture
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

        for number, slide in enumerate(presentation.slides, 1):
            text = "\n".join(shape_text(slide.shapes))
            add("SLIDE", number, text)
            if not text.strip() and contains_image(slide.shapes):
                ocr_suspected.append(number)
            if include_notes and slide.has_notes_slide:
                frame = slide.notes_slide.notes_text_frame
                if frame is not None and frame.text.strip():
                    add("NOTES", number, frame.text)
    else:
        raise DocumentRejected("DOCUMENT_INVALID")
    base = [span for span in spans if span.kind != "NOTES"]
    text_units = [span.position for span in base if span.text]
    blank = [span.position for span in base if not span.text and span.position not in ocr_suspected]
    content = _content_without_repeated_margins(base)
    useful_chars = sum(len("".join(text.split())) for text in content)
    useful = sum(bool(text) for text in content) >= MIN_USEFUL_UNITS and useful_chars >= MIN_USEFUL_CHARS
    if not ocr_suspected:
        # No unit looks scanned. Text-less sources stay blocked, but are not OCR candidates.
        status = "EXTRACTED" if any(span.text for span in spans) else "OCR_REQUIRED"
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
