from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ImportError(ValueError):
    """Raised when a local document cannot safely be imported."""


class FileOrganizationError(ValueError):
    """Raised when an explicit local file organization action cannot be completed safely."""


class SourceRelinkError(ValueError):
    """Raised when an explicit local source relink cannot be completed safely."""


@dataclass(frozen=True)
class SourceRoot:
    """One profile-approved directory available for an explicit source scan."""

    name: str
    path: Path
    status: str
    candidate_count: int
    imported_count: int


@dataclass(frozen=True)
class SourceCandidate:
    """One supported file discovered under an approved source root."""

    path: Path
    source_root: str


@dataclass(frozen=True)
class SourceImportPlan:
    """A reviewable, non-mutating discovery result for connected source roots."""

    roots: tuple[SourceRoot, ...]
    candidates: tuple[SourceCandidate, ...]


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
class ImportedDocument:
    id: int
    name: str
    page_count: int
    chunk_count: int
    ocr_page_count: int


@dataclass(frozen=True)
class ImportSummary:
    document: ImportedDocument
    semantic_index_status: str
    ocr_artifact_status: str


@dataclass(frozen=True)
class SourceImportFailure:
    name: str
    message: str


@dataclass(frozen=True)
class SourceImportResult:
    imported: tuple[ImportSummary, ...]
    failures: tuple[SourceImportFailure, ...]


@dataclass(frozen=True)
class CollectionHealth:
    document_count: int
    page_count: int
    chunk_count: int
    ocr_page_count: int
    unavailable_source_count: int
    changed_source_count: int
    baseline_reindex_count: int
    stale_chunking_count: int
    semantic_index_status: str
    source_attention: tuple["SourceAttention", ...]


@dataclass(frozen=True)
class SourceAttention:
    document_id: int
    document_name: str
    status: str


@dataclass(frozen=True)
class FileOrganizationPlan:
    document: ImportedDocument
    operation: str
    source_path: Path
    target_path: Path


@dataclass(frozen=True)
class SourceRelinkPlan:
    document: ImportedDocument
    previous_source_path: Path
    replacement_source_path: Path


@dataclass(frozen=True)
class ChunkInspection:
    id: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class PageInspection:
    page_number: int
    section: str | None
    extraction_method: str
    ocr_confidence: float | None
    chunks: tuple[ChunkInspection, ...]


@dataclass(frozen=True)
class DocumentInspection:
    document: ImportedDocument
    pages: tuple[PageInspection, ...]


@dataclass(frozen=True)
class Evidence:
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class DocumentRelationship:
    """One explainable cross-document similarity derived from local embeddings."""

    left_chunk: Chunk
    right_chunk: Chunk
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
