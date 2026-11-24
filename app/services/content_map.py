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

THIN_BODY_CHARS = 150
FORWARD_WINDOW = 4
BACKWARD_WINDOW = 12
MAX_ADDED_UNITS = 12

def body_chars(text: str) -> int:
    """Count prose characters, ignoring heading lines, page numbers and dot-leader TOC rows."""
    total = 0
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 4 or re.search(r"\.{3,}|…{2,}", line) or CHAPTER.match(line) or SECTION.match(line):
            continue
        total += len(line)
    return total

def expand_thin_scopes(records: dict[str, dict], meta: dict[str, tuple[str, str | None]],
                       texts: dict[tuple[str, int], str], units: list[tuple[str, int]],
                       unit_codes: dict[tuple[str, int], set[str]],
                       unit_chapters: dict[tuple[str, int], set[str]]) -> bool:
    """Widen heading nodes whose own pages are only a TOC/agenda row or a title-only slide.

    Retrieval is scoped to a node's source units, so a node that points only at an
    agenda page can never be supported. Candidates are, in order: a same-numbered
    heading where the real content starts, the contiguous pages that continue it, and
    finally the nearest substantive page before a run of title-only slides.
    """
    body = {unit: body_chars(text) for unit, text in texts.items()}
    pages = [unit for unit in units if unit[0] != "NOTES"]
    index = {unit: position for position, unit in enumerate(pages)}
    original = {key: set(record["source_units"]) for key, record in records.items()}

    def thin(selected: set) -> bool:
        return sum(body.get(unit, 0) for unit in selected) < THIN_BODY_CHARS

    changed = False
    for key, record in records.items():
        own = original[key]
        if record["origin"] != "HEADING" or key not in meta or not thin(own):
            continue
        code, scope = meta[key]
        anchors = [index[unit] for unit in own if unit in index]
        if not anchors:
            continue
        first, last = min(anchors), max(anchors)
        own_chapter = key if scope is None else scope
        # A later chapter heading in the node's last page closes its scope.
        if unit_chapters.get(pages[last], set()) - {own_chapter}:
            continue
        added: set[tuple[str, int]] = set()
        for other_key, other in records.items():
            if other_key != key and meta.get(other_key) == (code, scope) and other["origin"] == "HEADING" \
                    and not thin(original[other_key]) and min(index.get(u, -1) for u in original[other_key]) > last:
                added |= original[other_key] - own
        if thin(own | added):
            for unit in pages[last + 1:last + 1 + FORWARD_WINDOW]:
                if unit_chapters.get(unit, set()) - {own_chapter}:
                    break
                if any(other.count(".") <= code.count(".") for other in unit_codes.get(unit, ())):
                    break
                added.add(unit)
        if thin(own | added):
            for unit in reversed(pages[max(0, first - BACKWARD_WINDOW):first]):
                if body[unit] >= THIN_BODY_CHARS:
                    added.add(unit)
                    break
        added = set(sorted(added - own, key=lambda unit: (abs(index.get(unit, 0) - last), unit))[:MAX_ADDED_UNITS])
        if added:
            record["source_units"] |= added
            changed = True
    return changed

def reconstruct_texts(chunks: list[dict]) -> tuple[dict[tuple[str, int], str], dict]:
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
    return texts, identity

def content_map(index_id: UUID, title: str, chunks: list[dict]) -> OutlineResponse:
    """Build the reviewable topic outline from the reconstructed source units."""
    texts, identity = reconstruct_texts(chunks)
    units = sorted(texts, key=lambda unit: (unit[1], unit[0] == "NOTES", unit[0]))
    # Repeated page furniture is weak evidence for a knowledge topic.
    line_frequency = Counter(line.casefold().strip() for text in texts.values()
                             for line in set(text.splitlines()) if line.strip())
    records: dict[str, dict] = {}
    code_to_id: dict[tuple[str | None, str], str] = {}
    current_part: str | None = None
    active: list[str] = []
    headings = 0
    meta: dict[str, tuple[str, str | None]] = {}
    unit_codes: dict[tuple[str, int], set[str]] = defaultdict(set)
    unit_chapters: dict[tuple[str, int], set[str]] = defaultdict(set)


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
                meta[node] = (code, scope)
                unit_codes[unit].add(code)
                if chapter:
                    unit_chapters[unit].add(node)
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
    expanded = expand_thin_scopes(records, meta, texts, units, unit_codes, unit_chapters)
    disambiguated = disambiguate_siblings(records)
    nodes = [OutlineNode(**{**record, "source_units": [SourceUnit(kind=kind, position=position)
             for kind, position in sorted(record["source_units"], key=lambda unit: (unit[1], unit[0]))]}) for record in records.values()]
    return OutlineResponse(index_id=index_id, document_id=identity["document_id"], version_id=identity["version_id"],
                           source_sha256=identity["source_sha256"], nodes=nodes,
                           source_units=[SourceUnit(kind=kind, position=position) for kind, position in units],
                           warnings=["REVIEW_CONTENT_MAP", "OBJECTIVES_ARE_SUGGESTIONS"] + ([] if headings else ["INFERRED_TOPICS"])
                           + (["DUPLICATE_TOPIC_NAMES_DISAMBIGUATED"] if disambiguated else [])
                           + (["TOC_SCOPE_EXPANDED"] if expanded else []))

NO_TEXT_BODY_CHARS = 30

FACT_RELATION = re.compile(
    r"\b(?:is|are|was|were|uses?|provides?|requires?|allows?|prevents?|contains?|stores?|"
    r"returns?|supports?|exposes?|ensures?|removes?|means?|must|should|because|when|"
    r"là|gồm|chứa|dùng|giúp|cần|phải|khi|nếu|được|bằng|cho phép|sử dụng|bao gồm)\b", re.IGNORECASE)


def substantive_facts(text: str) -> set[str]:
    """Conservative local signals, not a claim of semantic sufficiency for generation."""
    facts: set[str] = set()
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        # Join wrapped prose only after a factual clause, never heading/bullet lists.
        if (lines and line and line[0].islower() and "|" not in line
                and FACT_RELATION.search(lines[-1]) and len(lines[-1].split()) >= 4
                and not re.search(r"[.!?;:]$", lines[-1])):
            lines[-1] += " " + line
        else:
            lines.append(line)
    for line in lines:
        if (not line or re.search(r"\.{3,}|…{2,}", line) or CHAPTER.match(line)
                or re.fullmatch(r"[\d\W]+", line) or re.search(r"https?://|www\.", line)):
            continue
        line = re.sub(r"^\s*(?:[-*•▪]+\s*|\d+(?:\.\d+)*[.)]?\s+)", "", line)
        if "|" in line:
            cells = [cell.strip() for cell in line.split("|") if cell.strip()]
            if len(cells) >= 3 and sum(len(cell) >= 5 for cell in cells) >= 2 and len(line) >= 45:
                facts.add(" ".join(line.casefold().split()))
            continue
        for statement in re.split(r"(?<=[.!?;])\s+(?=[A-ZÀ-Ỹ0-9])", line):
            words = re.findall(r"\w+", statement)
            if len(statement) >= 45 and len(words) >= 8 and FACT_RELATION.search(statement):
                facts.add(" ".join(statement.casefold().split()))
    return facts


def check_evidence(chunks: list[dict], items: list, ocr_candidate_units: list[int] | None = None,
                   ocr_pages: list | None = None) -> list[dict]:
    """Check complete scoped sources and type/level-aware factual depth without paid calls."""
    texts, _ = reconstruct_texts(chunks)
    pages = {page.position: page.status for page in ocr_pages or []}
    tracked = set(ocr_candidate_units or []) | set(pages)
    results = []
    thresholds = {"KNOWLEDGE": (1, 100), "UNDERSTANDING": (2, 180),
                  "APPLYING": (3, 260), "ADVANCED": (4, 360)}
    for item in items:
        units = {(unit.kind, unit.position) for unit in item.source_units}
        found = units & texts.keys()
        chars = sum(body_chars(texts[unit]) for unit in found)
        facts = set().union(*(substantive_facts(texts[unit]) for unit in found))
        substantive = sum(len(fact) for fact in facts)
        needed_facts, needed_chars = thresholds[item.question_format]
        if item.question_type == "MULTIPLE_CHOICE":
            needed_facts, needed_chars = max(2, needed_facts), max(200, needed_chars)
        incomplete = any(position in tracked and pages.get(position) not in {"COMPLETED", "NO_TEXT"}
                         for _, position in units)
        if incomplete:
            status = "OCR_INCOMPLETE"
        elif len(found) != len(units):
            status = "SOURCE_MISSING"
        elif chars < NO_TEXT_BODY_CHARS and not facts:
            status = "NO_TEXT"
        elif not facts:
            status = "TITLE_ONLY"
        elif substantive < 100:
            status = "THIN_TEXT"
        elif len(facts) < needed_facts or substantive < needed_chars:
            status = "INSUFFICIENT_DEPTH"
        else:
            status = "OK"
        results.append({"id": item.id, "status": status, "body_chars": chars, "unit_count": len(found),
                        "substantive_chars": substantive, "fact_count": len(facts)})
    return results
