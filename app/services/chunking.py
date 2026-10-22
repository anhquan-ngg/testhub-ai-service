"""Deterministic UTF-8-budgeted windows retaining exact original source offsets."""

import hashlib
from uuid import UUID, uuid5

from app.schemas.document import ExtractionResult
from app.schemas.rag import Chunk, MAX_CHUNKS, MAX_CHUNK_BYTES, TARGET_CHUNK_BYTES, OVERLAP_BYTES
from app.services.rag_errors import RagError


def byte_end(text: str, start: int, budget: int) -> int:
    """Return a Unicode boundary within a byte budget, without splitting UTF-8."""
    size, end = 0, start
    while end < len(text):
        width = len(text[end].encode("utf-8"))
        if size + width > budget:
            break
        size += width
        end += 1
    return end


def chunk_document(index_id: UUID, extraction: ExtractionResult) -> list[Chunk]:
    """Create stable chunks, never join different pages/slides/notes or alter text."""
    if extraction.status != "EXTRACTED":
        raise RagError("RAG_OCR_REQUIRED")
    chunks: list[Chunk] = []
    for span in extraction.spans:
        text, cursor = span.text, 0
        while cursor < len(text):
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor >= len(text):
                break
            maximum = byte_end(text, cursor, MAX_CHUNK_BYTES)
            target = byte_end(text, cursor, TARGET_CHUNK_BYTES)
            end = maximum
            if maximum < len(text):
                # Prefer paragraph/line/word boundaries near the target size.
                minimum = byte_end(text, cursor, TARGET_CHUNK_BYTES // 2)
                for separator in ("\n\n", "\n", ". ", " "):
                    boundary = text.rfind(separator, minimum, target + 1)
                    if boundary >= minimum:
                        end = boundary + (1 if separator == ". " else 0)
                        break
            while end > cursor and text[end-1].isspace():
                end -= 1
            if end <= cursor:
                raise RagError("RAG_CHUNKING_FAILED")
            content = text[cursor:end]
            digest = hashlib.sha256(content.encode()).hexdigest()
            ordinal = len(chunks)
            if ordinal >= MAX_CHUNKS:
                raise RagError("RAG_DOCUMENT_LIMIT")
            chunks.append(Chunk(
                chunk_id=uuid5(index_id, f"{ordinal}:{span.kind}:{span.position}:{cursor}:{end}:{digest}"),
                ordinal=ordinal, kind=span.kind, position=span.position,
                start_offset=cursor, end_offset=end, text=content, text_sha256=digest,
            ))
            if end >= len(text):
                break
            next_cursor, overlap = end, 0
            while next_cursor > cursor and overlap + len(text[next_cursor-1].encode()) <= OVERLAP_BYTES:
                next_cursor -= 1
                overlap += len(text[next_cursor].encode())
            cursor = next_cursor if next_cursor > cursor else end
    if not chunks:
        raise RagError("RAG_INSUFFICIENT_CONTEXT")
    return chunks
