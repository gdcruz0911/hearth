from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ImportError(ValueError):
    """Raised when a local document cannot safely be imported."""


@dataclass(frozen=True)
class ExtractedPage:
    page_number: int
    text: str
    section: str | None = None
    extraction_method: str = "native"
    ocr_confidence: float | None = None


@dataclass(frozen=True)
class SourceDocument:
    path: Path
    pages: tuple[ExtractedPage, ...]


@dataclass(frozen=True)
class Chunk:
    id: int
    document_id: int
    document_name: str
    page_number: int
    section: str | None
    text: str
    char_start: int
    char_end: int
    extraction_method: str
    ocr_confidence: float | None


@dataclass(frozen=True)
class Evidence:
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class Citation:
    document_name: str
    page_number: int
    section: str | None
    chunk_id: int
    quote: str
    extraction_method: str
    ocr_confidence: float | None


@dataclass(frozen=True)
class Answer:
    status: str
    text: str
    citations: tuple[Citation, ...]

    @classmethod
    def abstain(cls) -> "Answer":
        return cls(
            status="abstained",
            text="I cannot answer from the imported documents.",
            citations=(),
        )
