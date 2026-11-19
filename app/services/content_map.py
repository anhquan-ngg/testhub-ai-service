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


def disambiguate_siblings(records: dict[str, dict]) -> bool:
    """Make sibling names unique by qualifying them with their own heading line.

    Distinct headings may share a title (e.g. "Part 2: Normalization" and
    "3. Normalization"); they are different source topics, so they are kept
    apart instead of being merged.
    """
    groups: dict[tuple[str | None, str], list[str]] = defaultdict(list)
    for node_id, record in records.items():
        groups[(record["parent_id"], record["name"].casefold())].append(node_id)
    changed = False
    for members in (ids for ids in groups.values() if len(ids) > 1):
        taken = {records[i]["name"].casefold() for i in records if records[i]["parent_id"] == records[members[0]]["parent_id"]}
        for ordinal, node_id in enumerate(members, 1):
            record = records[node_id]
            label = topic_name(record["evidence"])[:200]
            candidate = label if label.casefold() != record["name"].casefold() and label.casefold() not in taken else ""
            suffix = ordinal
            while not candidate or candidate.casefold() in taken:
                candidate = f"{label[:190]} ({suffix})"
                suffix += 1
            taken.add(candidate.casefold())
            record["name"] = candidate
            changed = True
    return changed

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
    total_length = 0
    for unit, parts in by_unit.items():
        result: list[str] = []
        for part in sorted(parts, key=lambda value: value["start_offset"]):
            start, end, text = part["start_offset"], part["end_offset"], part["text"]
            if end - start != len(text):
                raise RagError("RAG_SOURCE_CHANGED", 409)
            gap = max(0, start - len(result))
            overlap = min(max(0, len(result) - start), len(text))
            growth = gap + len(text) - overlap
            if total_length + growth > 1_000_000:
                raise RagError("RAG_OUTLINE_LIMIT", 422)
            if "".join(result[start:start + overlap]) != text[:overlap]:
                raise RagError("RAG_SOURCE_CHANGED", 409)
            if gap:
                result.extend(" " * gap)
            result.extend(text[overlap:])
            total_length += growth
        texts[unit] = "".join(result)
    units = sorted(texts, key=lambda unit: (unit[1], unit[0] == "NOTES", unit[0]))
    # Repeated page furniture is weak evidence for a knowledge topic.
    line_frequency = Counter(line.casefold().strip() for text in texts.values()
                             for line in set(text.splitlines()) if line.strip())
    records: dict[str, dict] = {}
    code_to_id: dict[tuple[str | None, str], str] = {}
    current_part: str | None = None
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
                scope = None if chapter else current_part
                parent = None if chapter else current_part
                if not chapter:
                    for length in range(len(parts) - 1, 0, -1):
                        candidate = code_to_id.get((scope, ".".join(parts[:length])))
                        if candidate:
                            parent = candidate
                            break
                normalized = topic_name(name)
                previous = code_to_id.get((scope, code))
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
                code_to_id[(scope, code)] = node
                if chapter:
                    current_part = node
                    code_to_id[(current_part, code)] = node
                # Mark every heading's ancestors, even if another heading follows.
                active, cursor = [], node
                while cursor:
                    active.append(cursor)
                    records[cursor]["source_units"].add(unit)
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
    disambiguated = disambiguate_siblings(records)
    nodes = [OutlineNode(**{**record, "source_units": [SourceUnit(kind=kind, position=position)
             for kind, position in sorted(record["source_units"], key=lambda unit: (unit[1], unit[0]))]}) for record in records.values()]
    return OutlineResponse(index_id=index_id, document_id=identity["document_id"], version_id=identity["version_id"],
                           source_sha256=identity["source_sha256"], nodes=nodes,
                           source_units=[SourceUnit(kind=kind, position=position) for kind, position in units],
                           warnings=["REVIEW_CONTENT_MAP", "OBJECTIVES_ARE_SUGGESTIONS"] + ([] if headings else ["INFERRED_TOPICS"])
                           + (["DUPLICATE_TOPIC_NAMES_DISAMBIGUATED"] if disambiguated else []))
