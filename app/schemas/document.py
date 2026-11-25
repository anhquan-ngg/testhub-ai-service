"""Versioned extraction contract; source positions are one-based."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
MAX_UNITS = 500
PARSER_VERSION = "document-extraction.v1"


class SourceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["PAGE", "SLIDE", "NOTES"]
    position: int = Field(ge=1, le=MAX_UNITS)
    text: str = Field(max_length=MAX_TEXT_CHARS)


class OcrPage(BaseModel):
    """Observable OCR outcome; a nonempty text layer does not imply OCR completion."""
    model_config = ConfigDict(extra="forbid", strict=True)

    position: int = Field(ge=1, le=MAX_UNITS)
    status: Literal["PENDING", "COMPLETED", "NO_TEXT", "FAILED", "BUDGET_EXHAUSTED", "DISABLED"]
    reason: Literal["PAGE_LIMIT", "TIME_BUDGET", "DAILY_LIMIT", "PROVIDER_ERROR", "RENDER_FAILED",
                    "NO_ADDITIONAL_TEXT", "OCR_DISABLED", "CACHE_UNAVAILABLE", "TEXT_LIMIT"] | None = None


class ExtractionQuality(BaseModel):
    """Per-unit text classification; positions are original page/slide numbers."""
    model_config = ConfigDict(extra="forbid", strict=True)

    total_units: int = Field(ge=1, le=MAX_UNITS)
    text_units: int = Field(ge=0, le=MAX_UNITS)
    ocr_suspected_units: list[int] = Field(max_length=MAX_UNITS)
    blank_units: list[int] = Field(max_length=MAX_UNITS)
    # Units whose span text also contains a vision transcript of embedded images.
    ocr_units: list[int] = Field(default_factory=list, max_length=MAX_UNITS)
    ocr_candidate_units: list[int] = Field(default_factory=list, max_length=MAX_UNITS)
    ocr_pages: list[OcrPage] = Field(default_factory=list, max_length=MAX_UNITS)

class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    contract_version: Literal["document-extraction.v1"] = PARSER_VERSION
    source_type: Literal["PDF", "PPTX"]
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser_version: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9./;:_-]+$")
    include_notes: bool
    status: Literal["EXTRACTED", "PARTIAL", "OCR_REQUIRED"]
    unit_count: int = Field(ge=1, le=MAX_UNITS)
    spans: list[SourceSpan] = Field(min_length=1, max_length=MAX_UNITS * 2)
    warnings: list[Literal["EMPTY_UNITS", "OCR_REQUIRED", "PARTIAL_EXTRACTION", "NO_USABLE_TEXT"]] = Field(max_length=4)
    quality: ExtractionQuality | None = None

    @model_validator(mode="after")
    def check_manifest(self) -> "ExtractionResult":
        """Reject duplicate, missing or inconsistent source positions."""
        base_kind = "PAGE" if self.source_type == "PDF" else "SLIDE"
        base = [s.position for s in self.spans if s.kind == base_kind]
        if sorted(base) != list(range(1, self.unit_count + 1)):
            raise ValueError("Invalid source manifest")
        keys = [(s.kind, s.position) for s in self.spans]
        if len(set(keys)) != len(keys) or any(s.position > self.unit_count for s in self.spans):
            raise ValueError("Invalid source positions")
        if any(s.kind != base_kind and not (
            self.source_type == "PPTX" and self.include_notes and s.kind == "NOTES"
        ) for s in self.spans):
            raise ValueError("Invalid source kind")
        if sum(len(s.text) for s in self.spans) > MAX_TEXT_CHARS:
            raise ValueError("Text exceeds limit")
        if self.status in ("EXTRACTED", "PARTIAL") and not any(s.text.strip() for s in self.spans):
            raise ValueError("Empty extraction cannot succeed")
        if self.quality is not None:
            positions = self.quality.ocr_suspected_units + self.quality.blank_units
            # Notes provide usable text but are not page/slide text units.
            text_units = sum(bool(s.text.strip()) for s in self.spans if s.kind == base_kind)
            if (self.quality.total_units != self.unit_count
                or self.quality.text_units != text_units
                or len(set(positions)) != len(positions)
                or len(set(self.quality.ocr_units)) != len(self.quality.ocr_units)
                or len(set(self.quality.ocr_candidate_units)) != len(self.quality.ocr_candidate_units)
                or len({page.position for page in self.quality.ocr_pages}) != len(self.quality.ocr_pages)
                or any(not 1 <= position <= self.unit_count
                       for position in positions + self.quality.ocr_units + self.quality.ocr_candidate_units
                       + [page.position for page in self.quality.ocr_pages])):
                raise ValueError("Invalid extraction quality positions")
            if self.status == "PARTIAL" and not self.quality.ocr_suspected_units:
                raise ValueError("Partial extraction requires OCR-suspected units")
        return self
