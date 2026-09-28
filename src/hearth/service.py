from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

from .answering import EvidenceAnswerer, validate_answer
from .chunking import CHUNKING_VERSION, chunk_page
from .domain import (
    Answer,
    CollectionHealth,
    DocumentRelationship,
    Evidence,
    DocumentInspection,
    ImportedDocument,
    ImportError,
    ImportSummary,
    SourceCandidate,
    SourceAttention,
    SourceImportFailure,
    SourceImportPlan,
    SourceImportResult,
    SourceRoot,
)
from .embedding import FlatVectorIndex, IndexBusy
from .extraction import OCRmyPDFFallback, PageExtractor, PdfExtractor, PopplerPdfExtractor, TextNoteExtractor
from .retrieval import RRF_K, STOP_WORDS, IdentityReranker, Reranker, has_lexical_support, reciprocal_rank_fusion, terms
from .store import SQLiteStore


# Retrieval settings; trace() uses these and every evaluation record reports them.
KEYWORD_CANDIDATES = 20
SEMANTIC_CANDIDATES = 20
FUSED_CANDIDATES = 20
CITATIONS = 6


class HearthService:
    def __init__(
        self,
        database_path: Path,
        pdf_extractor: PageExtractor | None = None,
        reranker: Reranker | None = None,
        semantic_index: FlatVectorIndex | None = None,
        ocr_output_directory: Path | None = None,
        retain_ocr_output: bool = False,
        relationship_minimum_score: float = 0.72,
    ):
        if retain_ocr_output and ocr_output_directory is None:
            raise ValueError("retain_ocr_output requires ocr_output_directory.")
        if not 0 <= relationship_minimum_score <= 1:
            raise ValueError("relationship_minimum_score must be between zero and one.")
        self._store = SQLiteStore(database_path)
        self._note_extractor = TextNoteExtractor()
        native_pdf_extractor = PopplerPdfExtractor()
        self._pdf_extractor = pdf_extractor or PdfExtractor(
            native_extractor=native_pdf_extractor,
            ocr_fallback=(
                OCRmyPDFFallback(
                    native_pdf_extractor,
                    ocr_output_directory,
                    retain_output=retain_ocr_output,
                )
                if ocr_output_directory is not None
                else None
            ),
        )
        self._reranker = reranker or IdentityReranker()
        self._answerer = EvidenceAnswerer()
        self._semantic_index = semantic_index
        self._retain_ocr_output = retain_ocr_output
        self._relationship_minimum_score = relationship_minimum_score

    def close(self) -> None:
        self._store.close()

    def import_document(self, raw_path: str) -> int:
        return self._import_document(raw_path, rebuild_index=True)

    def _import_document(self, raw_path: str, *, rebuild_index: bool) -> int:
        path = _validated_local_file(raw_path)
        source_state = _source_state(path)
        extractor = self._pdf_extractor if path.suffix.lower() == ".pdf" else self._note_extractor
        document = extractor.extract(path)
        if _source_state(path) != source_state:
            raise ImportError("The source file changed during import. Retry after it finishes changing.")
        chunks_by_page = {page.page_number: chunk_page(page) for page in document.pages}
        if not any(chunks_by_page.values()):
            raise ImportError("No extractable text was found in the document.")
        document_id = self._store.replace_document(path, document.pages, chunks_by_page, *source_state)
        if rebuild_index:
            self._rebuild_semantic_index()
        return document_id

    def import_with_summary(self, raw_path: str) -> ImportSummary:
        return self._import_summary(self.import_document(raw_path))

    def remove_document(self, raw_path: str) -> bool:
        removed = self._store.remove_document(_validated_local_path(raw_path))
        if removed:
            self._rebuild_semantic_index()
        return removed

    def plan_remove_document(self, document_id: int) -> ImportedDocument:
        """Confirm one record exists before an explicit local-record removal."""
        inspection = self.inspect_document(document_id)
        if inspection is None:
            raise ImportError(f"No imported document with ID {document_id}.")
        return inspection.document

    def remove_document_by_id(self, document_id: int) -> bool:
        """Remove one collection record by ID without deleting its source file."""
        self.plan_remove_document(document_id)
        removed = self._store.remove_document_by_id(document_id)
        if removed:
            self._rebuild_semantic_index()
        return removed

    def reindex_document(self, raw_path: str) -> int:
        """Re-extract and replace all derived chunks for one local document."""
        return self.import_document(raw_path)

    def reindex_with_summary(self, raw_path: str) -> ImportSummary:
        return self._import_summary(self.reindex_document(raw_path))

    def plan_reindex_document(self, document_id: int) -> ImportedDocument:
        """Confirm the current source is available before an explicit reindex."""
        inspection = self.inspect_document(document_id)
        source_path = self._store.source_path(document_id)
        if inspection is None or source_path is None:
            raise ImportError(f"No imported document with ID {document_id}.")
        _validated_local_file(str(source_path))
        return inspection.document

    def reindex_document_by_id(self, document_id: int) -> ImportSummary:
        """Reindex a selected record while keeping its private path inside the service boundary."""
        self.plan_reindex_document(document_id)
        source_path = self._store.source_path(document_id)
        assert source_path is not None
        return self.reindex_with_summary(str(source_path))

    def list_documents(self) -> list[ImportedDocument]:
        """Return collection metadata without document text or canonical source paths."""
        return self._store.list_documents()

    def source_roots(self, source_roots: tuple[Path, ...]) -> tuple[SourceRoot, ...]:
        """Return connected-root availability without discovering or reading candidate files."""
        return tuple(
            SourceRoot(
                name=_source_root_name(root),
                path=root,
                status="ready" if root.is_dir() else "unavailable",
                candidate_count=0,
                imported_count=0,
            )
            for root in _unique_source_roots(source_roots)
        )

    def plan_source_import(self, source_roots: tuple[Path, ...]) -> SourceImportPlan:
        """Discover supported files only under profile-approved roots without reading their contents."""
        roots = _unique_source_roots(source_roots)
        imported_paths = {source_path for _, _, source_path, _, _, _ in self._store.source_records()}
        discovered_paths: set[Path] = set()
        root_statuses: list[SourceRoot] = []
        candidates: list[SourceCandidate] = []
        for root in roots:
            if not root.is_dir():
                root_statuses.append(SourceRoot(_source_root_name(root), root, "unavailable", 0, 0))
                continue
            root_candidates = _discover_source_candidates(root, imported_paths | discovered_paths)
            candidates.extend(root_candidates)
            discovered_paths.update(candidate.path for candidate in root_candidates)
            root_statuses.append(
                SourceRoot(
                    _source_root_name(root),
                    root,
                    "ready",
                    len(root_candidates),
                    sum(path.is_relative_to(root) for path in imported_paths),
                )
            )
        return SourceImportPlan(tuple(root_statuses), tuple(candidates))

    def import_source_plan(self, plan: SourceImportPlan) -> SourceImportResult:
        """Import an approved discovery plan, rebuilding derived vectors once after the batch."""
        roots = tuple(root.path for root in plan.roots if root.status == "ready")
        imported_document_ids: list[int] = []
        failures: list[SourceImportFailure] = []
        for candidate in plan.candidates:
            try:
                candidate_path = _validated_source_candidate(candidate.path, roots)
                imported_document_ids.append(self._import_document(str(candidate_path), rebuild_index=False))
            except ImportError as exc:
                failures.append(SourceImportFailure(candidate.path.name, str(exc)))
        if imported_document_ids:
            self._rebuild_semantic_index()
        return SourceImportResult(
            imported=tuple(self._import_summary(document_id) for document_id in imported_document_ids),
            failures=tuple(failures),
        )

    def inspect_document(self, document_id: int) -> DocumentInspection | None:
        """Return one document's page and chunk provenance without document text."""
        return self._store.inspect_document(document_id)

    def collection_health(self) -> CollectionHealth:
        semantic_index_status = "not configured"
        if self._semantic_index is not None:
            semantic_index_status = "ready" if self._semantic_index.is_current(self._store.list_chunks()) else "needs reindex"
        attention = []
        stale_chunking = self._store.stale_chunking_document_ids()
        for document_id, document_name, source_path, fingerprint, source_size, source_mtime_ns in self._store.source_records():
            status = _source_attention_status(source_path, fingerprint, source_size, source_mtime_ns)
            if status is None and document_id in stale_chunking:
                status = "chunking outdated"
            if status is not None:
                attention.append(SourceAttention(document_id, document_name, status))
        return self._store.collection_health(semantic_index_status, tuple(attention))

    def answer(self, question: str, *, keyword_only: bool = False) -> Answer:
        """Answer from cited evidence or abstain; keyword_only skips semantic search for exact lookups."""
        return self.trace(question, keyword_only=keyword_only)[0]

    def trace(self, question: str, *, keyword_only: bool = False) -> tuple[Answer, dict[str, object]]:
        """Answer exactly as answer() does, with a record of every stage for evaluation.

        The record holds chunk IDs and scores only, never chunk text: keyword ranks, semantic and fused scores,
        the reranker's score for every candidate, the citations, and the lexical-support gate's decision.
        """
        record: dict[str, object] = {
            "question": question, "keyword_only": keyword_only, "reranker": type(self._reranker).__name__,
            "keyword": [], "semantic": [], "fused": [], "reranked": [], "citations": [],
            "lexical_support": None, "status": "abstained",
        }
        if not question.strip():
            return Answer.abstain(), record
        chunks = self._store.list_chunks()
        # Hybrid retrieval: BM25 finds exact words such as flags and ADR numbers, embeddings find paraphrases.
        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        keyword = [Evidence(chunk=chunks_by_id[chunk_id], score=0.0) for chunk_id in self._store.keyword_search(terms(question), limit=KEYWORD_CANDIDATES)]
        record["keyword"] = [item.chunk.id for item in keyword]
        rankings = [keyword]
        if self._semantic_index is not None and not keyword_only:
            semantic = self._semantic_index.search(question, chunks, limit=SEMANTIC_CANDIDATES)
            record["semantic"] = _scored(semantic)
            rankings.append(semantic)
        candidates = reciprocal_rank_fusion(rankings, limit=FUSED_CANDIDATES)
        record["fused"] = _scored(candidates)
        # Rerank every candidate so each score is recorded; the six cited are the same six a limit of six returns.
        reranked = self._reranker.rerank(question, candidates, limit=len(candidates))
        record["reranked"] = _scored(reranked)
        evidence = reranked[:CITATIONS]
        answer = validate_answer(self._answerer.answer(question, evidence), evidence)
        if answer.status == "supported":
            record["lexical_support"] = has_lexical_support(question, [citation.quote for citation in answer.citations])
            if not record["lexical_support"]:
                answer = Answer.abstain()
        record["citations"] = [citation.chunk_id for citation in answer.citations]
        record["status"] = answer.status
        return answer, record

    def run_description(self) -> dict[str, object]:
        """What produces answers: retrieval settings, index and model identity, reranker, and the evidence gate."""
        describe = getattr(self._reranker, "describe", None)
        return {
            "retrieval": {
                "keyword_candidates": KEYWORD_CANDIDATES, "semantic_candidates": SEMANTIC_CANDIDATES,
                "fused_candidates": FUSED_CANDIDATES, "citations": CITATIONS, "fusion": "reciprocal rank fusion",
                "rrf_k": RRF_K, "keyword_index": "SQLite FTS5 BM25, porter unicode61", "chunking_version": CHUNKING_VERSION,
            },
            "semantic_index": self._semantic_index.describe() if self._semantic_index is not None else None,
            "reranker": describe() if describe is not None else {"name": type(self._reranker).__name__},
            "gate": {"rule": "ADR-0009: a cited quote shares a non-stopword term with the question",
                     "stop_words": sorted(STOP_WORDS)},
        }

    def document_relationships(self, *, limit: int = 12) -> list[DocumentRelationship]:
        """Return only relationships substantiated by the active local semantic index."""
        chunks = self._store.list_chunks()
        if self._semantic_index is None or not self._semantic_index.is_current(chunks):
            return []
        return self._semantic_index.document_relationships(
            chunks, limit=limit, minimum_score=self._relationship_minimum_score
        )

    def rebuild_semantic_index(
        self,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        on_warning: Callable[[str], None] | None = None,
    ) -> str:
        """Rebuild derived vectors from existing local evidence without changing source files."""
        if self._semantic_index is None:
            raise ImportError("Configure a local embedding model and index directory before building a semantic map.")
        self._semantic_index.rebuild(
            self._store.list_chunks(), on_progress=on_progress, is_cancelled=is_cancelled, on_warning=on_warning
        )
        return self.collection_health().semantic_index_status

    def _rebuild_semantic_index(self) -> None:
        if self._semantic_index is not None:
            try:
                self._semantic_index.rebuild(self._store.list_chunks())
            except IndexBusy:
                pass  # Another build is running; its manifest will not match these chunks, so health reports "needs reindex".

    def _import_summary(self, document_id: int) -> ImportSummary:
        inspection = self.inspect_document(document_id)
        if inspection is None:
            raise RuntimeError("Imported document could not be inspected.")
        if inspection.document.ocr_page_count == 0:
            ocr_artifact_status = "not used"
        elif self._retain_ocr_output:
            ocr_artifact_status = "retained locally for inspection"
        else:
            ocr_artifact_status = "temporary output deleted after extraction"
        return ImportSummary(
            document=inspection.document,
            semantic_index_status=self.collection_health().semantic_index_status,
            ocr_artifact_status=ocr_artifact_status,
        )

def _scored(items: list[Evidence]) -> list[list[float]]:
    return [[item.chunk.id, round(item.score, 5)] for item in items]


def _validated_local_file(raw_path: str) -> Path:
    path = _validated_local_path(raw_path)
    if not path.is_file():
        raise ImportError("Import path must be a regular file.")
    return path


def _validated_local_path(raw_path: str) -> Path:
    if not raw_path or "\x00" in raw_path:
        raise ImportError("A valid local file path is required.")
    try:
        return Path(raw_path).expanduser().resolve(strict=False)
    except OSError as exc:
        raise ImportError("A valid local file path is required.") from exc


def _unique_source_roots(source_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for root in source_roots:
        try:
            resolved = root.expanduser().resolve(strict=False)
        except OSError as exc:
            raise ImportError("A connected source root has an invalid local path.") from exc
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _source_root_name(root: Path) -> str:
    return root.name or str(root)


def _discover_source_candidates(root: Path, imported_paths: set[Path]) -> list[SourceCandidate]:
    candidates: list[SourceCandidate] = []
    supported_suffixes = TextNoteExtractor.supported_suffixes | {".pdf"}
    ignored_directories = {"node_modules", "__pycache__", ".hearth"}
    for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        directory_names[:] = [
            name
            for name in directory_names
            if not name.startswith(".") and name not in ignored_directories
        ]
        for file_name in sorted(file_names):
            if file_name.startswith(".") or Path(file_name).suffix.lower() not in supported_suffixes:
                continue
            candidate = Path(directory) / file_name
            if candidate.is_symlink():
                continue
            try:
                candidate = candidate.resolve(strict=True)
            except OSError:
                continue
            if not candidate.is_relative_to(root) or candidate in imported_paths:
                continue
            if candidate.is_file():
                candidates.append(SourceCandidate(candidate, _source_root_name(root)))
    return sorted(candidates, key=lambda item: (item.source_root.casefold(), str(item.path).casefold()))


def _validated_source_candidate(candidate: Path, roots: tuple[Path, ...]) -> Path:
    path = _validated_local_file(str(candidate))
    if not roots or not any(path.is_relative_to(root) for root in roots):
        raise ImportError("The reviewed source is no longer inside a connected folder. Preview again.")
    return path


def _source_state(path: Path) -> tuple[str, int, int]:
    try:
        metadata = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise ImportError("The local source file could not be read for import.") from exc
    return digest.hexdigest(), metadata.st_size, metadata.st_mtime_ns


def _source_attention_status(
    path: Path, fingerprint: str | None, source_size: int | None, source_mtime_ns: int | None
) -> str | None:
    if not path.is_file():
        return "source unavailable"
    if fingerprint is None or source_size is None or source_mtime_ns is None:
        return "source needs baseline reindex"
    try:
        metadata = path.stat()
    except OSError:
        return "source unavailable"
    if metadata.st_size == source_size and metadata.st_mtime_ns == source_mtime_ns:
        return None
    try:
        current_fingerprint, _, _ = _source_state(path)
    except ImportError:
        return "source unavailable"
    return None if current_fingerprint == fingerprint else "source changed since import"
