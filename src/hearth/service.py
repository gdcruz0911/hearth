from __future__ import annotations

from pathlib import Path

from .answering import EvidenceAnswerer, validate_answer
from .chunking import chunk_page
from .domain import Answer, ImportError
from .extraction import OcrmyPdfFallback, PageExtractor, PdfExtractor, PopplerPdfExtractor, TextNoteExtractor
from .retrieval import HashingVectorIndex, IdentityReranker, Reranker
from .store import SQLiteStore


class HearthService:
    def __init__(
        self,
        database_path: Path,
        pdf_extractor: PageExtractor | None = None,
        reranker: Reranker | None = None,
        ocr_output_directory: Path | None = None,
    ):
        self._store = SQLiteStore(database_path)
        self._note_extractor = TextNoteExtractor()
        native_pdf_extractor = PopplerPdfExtractor()
        self._pdf_extractor = pdf_extractor or PdfExtractor(
            native_extractor=native_pdf_extractor,
            ocr_fallback=(
                OcrmyPdfFallback(native_pdf_extractor, ocr_output_directory)
                if ocr_output_directory is not None
                else None
            ),
        )
        self._reranker = reranker or IdentityReranker()
        self._answerer = EvidenceAnswerer()

    def close(self) -> None:
        self._store.close()

    def import_document(self, raw_path: str) -> int:
        path = _validated_local_file(raw_path)
        extractor = self._pdf_extractor if path.suffix.lower() == ".pdf" else self._note_extractor
        document = extractor.extract(path)
        chunks_by_page = {page.page_number: chunk_page(page) for page in document.pages}
        if not any(chunks_by_page.values()):
            raise ImportError("No extractable text was found in the document.")
        return self._store.replace_document(path, document.pages, chunks_by_page)

    def remove_document(self, raw_path: str) -> bool:
        return self._store.remove_document(_validated_local_file(raw_path))

    def reindex_document(self, raw_path: str) -> int:
        """Re-extract and replace all derived chunks for one local document."""
        return self.import_document(raw_path)

    def answer(self, question: str) -> Answer:
        if not question.strip():
            return Answer.abstain()
        candidates = HashingVectorIndex(self._store.list_chunks()).search(question, limit=20)
        evidence = self._reranker.rerank(question, candidates, limit=6)
        return validate_answer(self._answerer.answer(evidence), evidence)


def _validated_local_file(raw_path: str) -> Path:
    if not raw_path or "\x00" in raw_path:
        raise ImportError("A valid local file path is required.")
    path = Path(raw_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ImportError("Import path must be a regular file.")
    return path
