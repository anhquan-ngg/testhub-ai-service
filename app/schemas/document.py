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


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    contract_version: Literal["document-extraction.v1"] = PARSER_VERSION
    source_type: Literal["PDF", "PPTX"]
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser_version: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9./;:_-]+$")
    include_notes: bool
    status: Literal["EXTRACTED", "OCR_REQUIRED"]
    unit_count: int = Field(ge=1, le=MAX_UNITS)
    spans: list[SourceSpan] = Field(min_length=1, max_length=MAX_UNITS * 2)
    warnings: list[Literal["EMPTY_UNITS", "OCR_REQUIRED"]] = Field(max_length=2)

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
        if self.status == "EXTRACTED" and not any(s.text.strip() for s in self.spans):
            raise ValueError("Empty extraction cannot succeed")
        return self
