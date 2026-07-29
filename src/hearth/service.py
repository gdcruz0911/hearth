from __future__ import annotations

from pathlib import Path

from .answering import Answerer, EvidenceAnswerer, validate_answer
from .chunking import chunk_page
from .domain import Answer, DocumentInspection, ImportedDocument, ImportError
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
    ):
        self._store = SQLiteStore(database_path)
        self._note_extractor = TextNoteExtractor()
        native_pdf_extractor = PopplerPdfExtractor()
        self._pdf_extractor = pdf_extractor or PdfExtractor(
            native_extractor=native_pdf_extractor,
            ocr_fallback=(
                OCRmyPDFFallback(native_pdf_extractor, ocr_output_directory)
                if ocr_output_directory is not None
                else None
            ),
        )
        self._reranker = reranker or IdentityReranker()
        self._answerer = answerer or EvidenceAnswerer()
        self._semantic_index = semantic_index

    def close(self) -> None:
        self._store.close()

    def import_document(self, raw_path: str) -> int:
        path = _validated_local_file(raw_path)
        extractor = self._pdf_extractor if path.suffix.lower() == ".pdf" else self._note_extractor
        document = extractor.extract(path)
        chunks_by_page = {page.page_number: chunk_page(page) for page in document.pages}
        if not any(chunks_by_page.values()):
            raise ImportError("No extractable text was found in the document.")
        document_id = self._store.replace_document(path, document.pages, chunks_by_page)
        self._rebuild_semantic_index()
        return document_id

    def remove_document(self, raw_path: str) -> bool:
        removed = self._store.remove_document(_validated_local_file(raw_path))
        if removed:
            self._rebuild_semantic_index()
        return removed

    def reindex_document(self, raw_path: str) -> int:
        """Re-extract and replace all derived chunks for one local document."""
        return self.import_document(raw_path)

    def list_documents(self) -> list[ImportedDocument]:
        """Return collection metadata without document text or canonical source paths."""
        return self._store.list_documents()

    def inspect_document(self, document_id: int) -> DocumentInspection | None:
        """Return one document's page and chunk provenance without document text."""
        return self._store.inspect_document(document_id)

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


def _validated_local_file(raw_path: str) -> Path:
    if not raw_path or "\x00" in raw_path:
        raise ImportError("A valid local file path is required.")
    path = Path(raw_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ImportError("Import path must be a regular file.")
    return path
