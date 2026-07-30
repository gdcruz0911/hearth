from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .answering import Answerer, EvidenceAnswerer, validate_answer
from .chunking import chunk_page
from .domain import (
    Answer,
    CollectionHealth,
    DocumentInspection,
    FileOrganizationError,
    FileOrganizationPlan,
    ImportedDocument,
    ImportError,
    ImportSummary,
    SourceAttention,
    SourceRelinkError,
    SourceRelinkPlan,
)
from .embedding import FlatVectorIndex
from .extraction import OCRmyPDFFallback, PageExtractor, PdfExtractor, PopplerPdfExtractor, TextNoteExtractor
from .retrieval import HashingVectorIndex, IdentityReranker, Reranker, has_lexical_support
from .store import SQLiteStore


class HearthService:
    def __init__(
        self,
        database_path: Path,
        pdf_extractor: PageExtractor | None = None,
        reranker: Reranker | None = None,
        answerer: Answerer | None = None,
        semantic_index: FlatVectorIndex | None = None,
        ocr_output_directory: Path | None = None,
        retain_ocr_output: bool = False,
    ):
        if retain_ocr_output and ocr_output_directory is None:
            raise ValueError("retain_ocr_output requires ocr_output_directory.")
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
        self._answerer = answerer or EvidenceAnswerer()
        self._semantic_index = semantic_index
        self._retain_ocr_output = retain_ocr_output

    def close(self) -> None:
        self._store.close()

    def import_document(self, raw_path: str) -> int:
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

    def inspect_document(self, document_id: int) -> DocumentInspection | None:
        """Return one document's page and chunk provenance without document text."""
        return self._store.inspect_document(document_id)

    def collection_health(self) -> CollectionHealth:
        semantic_index_status = "not configured"
        if self._semantic_index is not None:
            semantic_index_status = "ready" if self._semantic_index.is_current(self._store.list_chunks()) else "needs reindex"
        attention = []
        for document_id, document_name, source_path, fingerprint, source_size, source_mtime_ns in self._store.source_records():
            status = _source_attention_status(source_path, fingerprint, source_size, source_mtime_ns)
            if status is not None:
                attention.append(SourceAttention(document_id, document_name, status))
        return self._store.collection_health(semantic_index_status, tuple(attention))

    def plan_organization(
        self, document_id: int, *, move_to: Path | None = None, rename: str | None = None
    ) -> FileOrganizationPlan:
        if (move_to is None) == (rename is None):
            raise FileOrganizationError("Choose exactly one of move_to or rename.")
        inspection = self.inspect_document(document_id)
        source_path = self._store.source_path(document_id)
        if inspection is None or source_path is None:
            raise FileOrganizationError(f"No imported document with ID {document_id}.")
        if not source_path.is_file():
            raise FileOrganizationError("The imported source file is unavailable. Restore it before organizing.")
        if rename is not None:
            target_path = _rename_target(source_path, rename)
            operation = "rename"
        else:
            target_path = _move_target(source_path, move_to)
            operation = "move"
        _validate_organization_target(source_path, target_path)
        return FileOrganizationPlan(
            document=inspection.document,
            operation=operation,
            source_path=source_path,
            target_path=target_path,
        )

    def apply_organization(
        self, document_id: int, *, move_to: Path | None = None, rename: str | None = None
    ) -> FileOrganizationPlan:
        plan = self.plan_organization(document_id, move_to=move_to, rename=rename)
        _move_without_overwrite(plan.source_path, plan.target_path)
        try:
            self._store.relocate_document(document_id, plan.source_path, plan.target_path)
        except Exception as exc:
            try:
                _move_without_overwrite(plan.target_path, plan.source_path)
            except FileOrganizationError as rollback_error:
                raise FileOrganizationError(
                    "The file moved but Hearth could not update or restore its source binding."
                ) from rollback_error
            raise FileOrganizationError("Hearth restored the file because its source binding could not be updated.") from exc
        return plan

    def plan_relink(self, document_id: int, replacement_source_path: Path) -> SourceRelinkPlan:
        plan, _ = self._relink_plan(document_id, replacement_source_path)
        return plan

    def apply_relink(self, document_id: int, replacement_source_path: Path) -> SourceRelinkPlan:
        plan, source_fingerprint = self._relink_plan(document_id, replacement_source_path)
        try:
            replacement_state = _source_state(plan.replacement_source_path)
        except ImportError as exc:
            raise SourceRelinkError("The replacement source file could not be read.") from exc
        if replacement_state[0] != source_fingerprint:
            raise SourceRelinkError("The replacement source changed while relinking. Retry after it finishes changing.")
        try:
            self._store.relink_document(
                document_id,
                plan.previous_source_path,
                plan.replacement_source_path,
                source_fingerprint,
                replacement_state[1],
                replacement_state[2],
            )
        except ValueError as exc:
            raise SourceRelinkError(str(exc)) from exc
        return plan

    def answer(self, question: str) -> Answer:
        if not question.strip():
            return Answer.abstain()
        chunks = self._store.list_chunks()
        candidates = (
            self._semantic_index.search(question, chunks, limit=20)
            if self._semantic_index is not None
            else HashingVectorIndex(chunks).search(question, limit=20)
        )
        evidence = self._reranker.rerank(question, candidates, limit=6)
        answer = validate_answer(self._answerer.answer(question, evidence), evidence)
        if answer.status == "supported" and not has_lexical_support(
            question, [citation.quote for citation in answer.citations]
        ):
            return Answer.abstain()
        return answer

    def _rebuild_semantic_index(self) -> None:
        if self._semantic_index is not None:
            self._semantic_index.rebuild(self._store.list_chunks())

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

    def _relink_plan(self, document_id: int, replacement_source_path: Path) -> tuple[SourceRelinkPlan, str]:
        inspection = self.inspect_document(document_id)
        previous_source_path = self._store.source_path(document_id)
        source_fingerprint = self._store.source_fingerprint(document_id)
        if inspection is None or previous_source_path is None:
            raise SourceRelinkError(f"No imported document with ID {document_id}.")
        if previous_source_path.is_file():
            raise SourceRelinkError("The imported source file is still available. Relink is only for an unavailable source.")
        if source_fingerprint is None:
            raise SourceRelinkError(
                "This source needs a baseline reindex before it can be relinked. Restore it, then run reindex."
            )
        try:
            replacement_path = _validated_local_file(str(replacement_source_path))
            replacement_fingerprint, _, _ = _source_state(replacement_path)
        except ImportError as exc:
            raise SourceRelinkError("Choose a readable local replacement file.") from exc
        if replacement_fingerprint != source_fingerprint:
            raise SourceRelinkError(
                "The replacement source does not match the imported source fingerprint. Reindex it to refresh extracted content."
            )
        return (
            SourceRelinkPlan(
                document=inspection.document,
                previous_source_path=previous_source_path,
                replacement_source_path=replacement_path,
            ),
            source_fingerprint,
        )


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


def _rename_target(source_path: Path, rename: str) -> Path:
    candidate = Path(rename)
    if not rename or candidate.name != rename or rename in {".", ".."}:
        raise FileOrganizationError("The new name must be a single file name.")
    if candidate.suffix != source_path.suffix:
        raise FileOrganizationError("Renaming must preserve the source file extension.")
    return source_path.with_name(rename)


def _move_target(source_path: Path, move_to: Path | None) -> Path:
    assert move_to is not None
    try:
        target_directory = move_to.expanduser().resolve(strict=True)
    except OSError as exc:
        raise FileOrganizationError("The target directory is unavailable.") from exc
    if not target_directory.is_dir():
        raise FileOrganizationError("The move target must be an existing directory.")
    return target_directory / source_path.name


def _validate_organization_target(source_path: Path, target_path: Path) -> None:
    if source_path == target_path:
        raise FileOrganizationError("The target must differ from the current source path.")
    if os.path.lexists(target_path):
        raise FileOrganizationError("The target path already exists. Hearth will not overwrite files.")
    try:
        if source_path.stat().st_dev != target_path.parent.stat().st_dev:
            raise FileOrganizationError("Cross-volume moves are not supported by the first organization version.")
    except OSError as exc:
        raise FileOrganizationError("The source or target directory is unavailable.") from exc


def _move_without_overwrite(source_path: Path, target_path: Path) -> None:
    """Moves one same-volume file without allowing a target replacement race."""
    try:
        os.link(source_path, target_path)
    except FileExistsError as exc:
        raise FileOrganizationError("The target path already exists. Hearth will not overwrite files.") from exc
    except OSError as exc:
        raise FileOrganizationError("The local file could not be moved or renamed.") from exc
    try:
        source_path.unlink()
    except OSError as exc:
        try:
            target_path.unlink()
        except OSError as rollback_error:
            raise FileOrganizationError(
                "Hearth could not move the file and could not restore the original source state."
            ) from rollback_error
        raise FileOrganizationError("Hearth restored the source because the file move could not complete.") from exc
