"""Read ALL source units, prefer numbered headings, and mark inferred topics explicitly."""
import re
import unicodedata
from collections import Counter, defaultdict
from uuid import UUID, uuid5
from app.schemas.knowledge_plan import OutlineNode, OutlineResponse, SourceUnit
from app.services.rag_errors import RagError

CHAPTER = re.compile(r"^\s*(?i:chương|chapter|phần|part)\s*(\d+|[IVXLCDM]+)\b\s*[.：:\-–]?\s*(.{0,190})$")
SECTION = re.compile(r"^\s*((?:\d+\.)*\d+)[.)]?\s+([^\d\W].{2,190})$", re.UNICODE)
CONTINUATION = re.compile(r"\s*\(\s*[1-9]\d*\s*\)\s*$")


def topic_name(name: str) -> str:
    """Normalize slide continuation counters, preserving other parenthetical qualifiers."""
    normalized = " ".join(unicodedata.normalize("NFKC", name).split())
    base = CONTINUATION.sub("", normalized).strip()
    return base if base and any(char.isalpha() for char in base) else normalized


def content_map(index_id: UUID, title: str, chunks: list[dict]) -> OutlineResponse:
    """Reconstruct overlapping chunks by Unicode offsets, never by a top-k query sample."""
    if not chunks or len(chunks) > 5000:
        raise RagError("RAG_OUTLINE_UNAVAILABLE", 422)
    by_unit: dict[tuple[str, int], list[dict]] = defaultdict(list)
    identity = chunks[0]
    for chunk in chunks:
        if any(chunk[key] != identity[key] for key in ("document_id", "version_id", "source_sha256")):
            raise RagError("RAG_SOURCE_CHANGED", 409)
        by_unit[(chunk["kind"], chunk["position"])].append(chunk)
    texts: dict[tuple[str, int], str] = {}
    for unit, parts in by_unit.items():
        result: list[str] = []
        for part in sorted(parts, key=lambda value: value["start_offset"]):
            start, end, text = part["start_offset"], part["end_offset"], part["text"]
            if end - start != len(text):
                raise RagError("RAG_SOURCE_CHANGED", 409)
            if start > len(result):
                result.extend(" " * (start - len(result)))
            overlap = min(len(result) - start, len(text))
            if "".join(result[start:start + overlap]) != text[:overlap]:
                raise RagError("RAG_SOURCE_CHANGED", 409)
            result.extend(text[overlap:])
        texts[unit] = "".join(result)
    if sum(map(len, texts.values())) > 1_000_000:
        raise RagError("RAG_OUTLINE_LIMIT", 422)
    units = sorted(texts, key=lambda unit: (unit[1], unit[0] == "NOTES", unit[0]))
    # Repeated page furniture is weak evidence for a knowledge topic.
    line_frequency = Counter(line.casefold().strip() for text in texts.values()
                             for line in set(text.splitlines()) if line.strip())
    records: dict[str, dict] = {}
    code_to_id: dict[str, str] = {}
    active: list[str] = []
    headings = 0
    

    def add(name: str, parent: str | None, unit: tuple[str, int], origin: str, evidence: str, key: str) -> str:
        normalized = topic_name(name)[:200]
        node_id = str(uuid5(index_id, key))
        if node_id not in records:
            if len(records) >= 1000:
                raise RagError("RAG_OUTLINE_LIMIT", 422)
            records[node_id] = {"id": node_id, "parent_id": parent, "name": normalized,
                                "source_units": set(), "origin": origin, "evidence": evidence.strip()[:200]}
        records[node_id]["source_units"].add(unit)
        return node_id

    for unit in units:
        text = texts[unit]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        found = False
        if unit[0] != "NOTES":
            for line_index, line in enumerate(lines):
                # Dot leaders/page numbers belong to a TOC, not repeated chapter instances.
                if re.search(r"\.{3,}|…{2,}", line) or len(line) > 200:
                    continue
                chapter = CHAPTER.match(line)
                section = SECTION.match(line)
                if not chapter and not section:
                    continue
                found = True
                headings += 1
                code, name = (chapter or section).groups()
                if chapter and not name.strip():
                    following = lines[line_index + 1] if line_index + 1 < len(lines) else ""
                    name = following if 2 <= len(following) <= 190 and not CHAPTER.match(following) and not SECTION.match(following) else line
                parts = code.split(".")
                parent = None
                if not chapter:
                    for length in range(len(parts) - 1, 0, -1):
                        parent = code_to_id.get(".".join(parts[:length]))
                        if parent:
                            break
                normalized = topic_name(name)
                previous = code_to_id.get(code)
                # A numbered continuation may shorten the first title (e.g.
                # "Abstraction in OOP" -> "Abstraction (3)"). Keep that first
                # title only when both the section and parent match.
                if previous and CONTINUATION.search(unicodedata.normalize("NFKC", name)):
                    record = records[previous]
                    first = record["name"].casefold()
                    if record["parent_id"] == parent and (first == normalized.casefold() or first.startswith(normalized.casefold() + " ")):
                        normalized = record["name"]
                node = add(normalized, parent, unit, "HEADING", line,
                           "heading:" + (parent or "root") + ":" + code + ":" + normalized.casefold())
                code_to_id[code] = node
                # Iterative ancestor walk: no configured hierarchy-depth limit.
                active, cursor = [], node
                while cursor:
                    active.append(cursor)
                    cursor = records[cursor]["parent_id"]
        if not found and not active:
            candidates = [line for line in lines if len(line) >= 4 and any(char.isalpha() for char in line)]
            evidence = next((line for line in candidates if line_frequency[line.casefold()] <= max(1, len(units) // 2)),
                            candidates[0] if candidates else title)
            root = add(title, None, unit, "INFERRED", evidence, "document-root")
            name = topic_name(evidence)[:160]
            node = add(name, root, unit, "INFERRED", evidence,
                       "inferred:" + root + ":" + name.casefold())
            # Inferred slide/page topics do not swallow all subsequent units.
            active = [node, root]
        for node in active:
            records[node]["source_units"].add(unit)
        if records.get(active[0] if active else "", {}).get("origin") == "INFERRED":
            active = []
    nodes = [OutlineNode(**{**record, "source_units": [SourceUnit(kind=kind, position=position)
             for kind, position in sorted(record["source_units"], key=lambda unit: (unit[1], unit[0]))]}) for record in records.values()]
    return OutlineResponse(index_id=index_id, document_id=identity["document_id"], version_id=identity["version_id"],
                           source_sha256=identity["source_sha256"], nodes=nodes,
                           source_units=[SourceUnit(kind=kind, position=position) for kind, position in units],
                           warnings=["REVIEW_CONTENT_MAP", "OBJECTIVES_ARE_SUGGESTIONS"] + ([] if headings else ["INFERRED_TOPICS"]))
