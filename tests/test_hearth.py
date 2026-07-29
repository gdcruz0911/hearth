from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hearth.domain import Evidence, ExtractedPage, SourceDocument
from hearth.embedding import EmbeddingSpec, FlatVectorIndex
from hearth.extraction import PdfExtractor
from hearth.service import HearthService


class FakePdfExtractor:
    def extract(self, path: Path) -> SourceDocument:
        return SourceDocument(
            path=path,
            pages=(
                ExtractedPage(1, "The project review is on Tuesday.", section="Schedule"),
                ExtractedPage(2, "The backup location is encrypted local storage.", section="Security"),
            ),
        )


class NativePdfExtractorWithBlankPage:
    def extract(self, path: Path) -> SourceDocument:
        return SourceDocument(
            path=path,
            pages=(
                ExtractedPage(1, "The account is active.", section="Native"),
                ExtractedPage(2, "", section="Scanned"),
            ),
        )


class FakeOcrFallback:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, tuple[int, ...]]] = []

    def extract_pages(self, pdf_path: Path, page_numbers: tuple[int, ...]) -> dict[int, ExtractedPage]:
        self.calls.append((pdf_path, page_numbers))
        return {
            2: ExtractedPage(
                2,
                "The scanned archive is stored locally.",
                section="Scanned",
                extraction_method="ocr",
                ocr_confidence=0.92,
            )
        }


class FakeSemanticIndex:
    def __init__(self) -> None:
        self.rebuild_chunk_ids: list[tuple[int, ...]] = []

    def rebuild(self, chunks) -> None:
        self.rebuild_chunk_ids.append(tuple(chunk.id for chunk in chunks))

    def search(self, question: str, chunks, limit: int = 20):
        return []


class SemanticIndexReturningFirstChunk(FakeSemanticIndex):
    def search(self, question: str, chunks, limit: int = 20):
        return [Evidence(chunks[0], 0.9)] if chunks else []


class FakeFlatEmbedder:
    spec = EmbeddingSpec(
        model_name="synthetic-embedding-model",
        model_fingerprint="synthetic-fingerprint",
        dimension=2,
        pooling="last-token",
        normalization="l2",
    )

    def __init__(self, vectors: dict[str, list[float]]):
        self._vectors = vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors[text] for text in texts]


class HearthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "hearth.sqlite"
        self.note = self.root / "facts.md"
        self.note.write_text("# Operations\n\nThe deployment owner is Ada.\n", encoding="utf-8")
        self.service = HearthService(self.database)

    def tearDown(self) -> None:
        self.service.close()
        self.temporary_directory.cleanup()

    def test_answer_cites_exact_note_page_and_section(self) -> None:
        self.service.import_document(str(self.note))

        answer = self.service.answer("Who is the deployment owner?")

        self.assertEqual(answer.status, "supported")
        self.assertEqual(answer.citations[0].document_name, "facts.md")
        self.assertEqual(answer.citations[0].page_number, 1)
        self.assertEqual(answer.citations[0].section, "Operations")
        self.assertIn("Ada", answer.citations[0].quote)

    def test_unsupported_question_abstains(self) -> None:
        self.service.import_document(str(self.note))

        answer = self.service.answer("What is the annual budget?")

        self.assertEqual(answer.status, "abstained")
        self.assertEqual(answer.citations, ())

    def test_semantic_retrieval_without_question_term_support_abstains(self) -> None:
        self.service.close()
        self.service = HearthService(self.database, semantic_index=SemanticIndexReturningFirstChunk())
        self.service.import_document(str(self.note))

        answer = self.service.answer("What is the annual budget?")

        self.assertEqual(answer.status, "abstained")
        self.assertEqual(answer.citations, ())

    def test_semantic_retrieval_with_question_term_support_answers(self) -> None:
        self.service.close()
        self.service = HearthService(self.database, semantic_index=SemanticIndexReturningFirstChunk())
        self.service.import_document(str(self.note))

        answer = self.service.answer("Who is the deployment owner?")

        self.assertEqual(answer.status, "supported")
        self.assertIn("Ada", answer.text)

    def test_removal_deletes_searchable_content(self) -> None:
        self.service.import_document(str(self.note))

        self.assertTrue(self.service.remove_document(str(self.note)))
        self.assertEqual(self.service.answer("Who is the deployment owner?").status, "abstained")

    def test_reindex_replaces_prior_indexed_content(self) -> None:
        self.service.reindex_document(str(self.note))
        self.note.write_text("# Operations\n\nThe deployment owner is Lin.\n", encoding="utf-8")

        self.service.import_document(str(self.note))
        answer = self.service.answer("Who is the deployment owner?")

        self.assertEqual(answer.status, "supported")
        self.assertIn("Lin", answer.text)
        self.assertNotIn("Ada", answer.text)

    def test_pdf_pages_keep_page_metadata(self) -> None:
        pdf = self.root / "handbook.pdf"
        pdf.write_bytes(b"placeholder")
        self.service.close()
        self.service = HearthService(self.database, pdf_extractor=FakePdfExtractor())
        self.service.import_document(str(pdf))

        answer = self.service.answer("Where is the backup location?")

        self.assertEqual(answer.status, "supported")
        self.assertEqual(answer.citations[0].page_number, 2)
        self.assertEqual(answer.citations[0].section, "Security")

    def test_ocr_fallback_replaces_only_blank_pages_and_marks_citation(self) -> None:
        pdf = self.root / "scanned.pdf"
        pdf.write_bytes(b"placeholder")
        fallback = FakeOcrFallback()
        extractor = PdfExtractor(NativePdfExtractorWithBlankPage(), fallback)
        self.service.close()
        self.service = HearthService(self.database, pdf_extractor=extractor)

        self.service.import_document(str(pdf))
        answer = self.service.answer("Where is the scanned archive stored?")

        self.assertEqual(fallback.calls, [(pdf.resolve(), (2,))])
        self.assertEqual(answer.status, "supported")
        self.assertEqual(answer.citations[0].page_number, 2)
        self.assertEqual(answer.citations[0].extraction_method, "ocr")
        self.assertEqual(answer.citations[0].ocr_confidence, 0.92)

    def test_ocr_reindex_replaces_records_and_rebuilds_semantic_index(self) -> None:
        pdf = self.root / "scanned.pdf"
        pdf.write_bytes(b"placeholder")
        fallback = FakeOcrFallback()
        semantic_index = FakeSemanticIndex()
        extractor = PdfExtractor(NativePdfExtractorWithBlankPage(), fallback)
        self.service.close()
        self.service = HearthService(
            self.database,
            pdf_extractor=extractor,
            semantic_index=semantic_index,
        )

        self.service.import_document(str(pdf))
        document_id = self.service.reindex_document(str(pdf))
        documents = self.service.list_documents()
        inspection = self.service.inspect_document(document_id)

        self.assertEqual(fallback.calls, [(pdf.resolve(), (2,)), (pdf.resolve(), (2,))])
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].id, document_id)
        self.assertEqual(documents[0].page_count, 2)
        self.assertEqual(documents[0].ocr_page_count, 1)
        self.assertIsNotNone(inspection)
        assert inspection is not None
        self.assertEqual(inspection.pages[1].extraction_method, "ocr")
        self.assertEqual(len(semantic_index.rebuild_chunk_ids), 2)
        self.assertTrue(all(chunk_ids for chunk_ids in semantic_index.rebuild_chunk_ids))

    def test_semantic_index_rebuilds_after_import_and_removal(self) -> None:
        semantic_index = FakeSemanticIndex()
        self.service.close()
        self.service = HearthService(self.database, semantic_index=semantic_index)

        self.service.import_document(str(self.note))
        self.service.remove_document(str(self.note))

        self.assertEqual(len(semantic_index.rebuild_chunk_ids), 2)
        self.assertTrue(semantic_index.rebuild_chunk_ids[0])
        self.assertEqual(semantic_index.rebuild_chunk_ids[1], ())

    def test_collection_inspection_returns_metadata_without_document_text_or_source_path(self) -> None:
        document_id = self.service.import_document(str(self.note))

        documents = self.service.list_documents()
        inspection = self.service.inspect_document(document_id)

        self.assertEqual(documents[0].id, document_id)
        self.assertEqual(documents[0].name, "facts.md")
        self.assertEqual(documents[0].page_count, 1)
        self.assertEqual(documents[0].chunk_count, 1)
        self.assertEqual(documents[0].ocr_page_count, 0)
        self.assertIsNotNone(inspection)
        assert inspection is not None
        self.assertEqual(inspection.pages[0].page_number, 1)
        self.assertEqual(inspection.pages[0].section, "Operations")
        self.assertEqual(inspection.pages[0].extraction_method, "native")
        self.assertEqual(len(inspection.pages[0].chunks), 1)
        self.assertFalse(hasattr(inspection, "text"))
        self.assertFalse(hasattr(inspection.pages[0].chunks[0], "text"))

    def test_collection_inspection_marks_ocr_pages_for_review(self) -> None:
        pdf = self.root / "scanned.pdf"
        pdf.write_bytes(b"placeholder")
        extractor = PdfExtractor(NativePdfExtractorWithBlankPage(), FakeOcrFallback())
        self.service.close()
        self.service = HearthService(self.database, pdf_extractor=extractor)

        document_id = self.service.import_document(str(pdf))
        document = self.service.list_documents()[0]
        inspection = self.service.inspect_document(document_id)

        self.assertEqual(document.ocr_page_count, 1)
        self.assertIsNotNone(inspection)
        assert inspection is not None
        ocr_page = inspection.pages[1]
        self.assertEqual(ocr_page.extraction_method, "ocr")
        self.assertEqual(ocr_page.ocr_confidence, 0.92)
        self.assertEqual(len(ocr_page.chunks), 1)

    def test_flat_semantic_index_reindex_and_removal_replace_active_content(self) -> None:
        initial_text = "The deployment owner is Ada."
        updated_text = "The deployment owner is Lin."
        question = "Who is the deployment owner?"
        self.note.write_text(initial_text, encoding="utf-8")
        index = FlatVectorIndex(
            self.root / "semantic-index",
            FakeFlatEmbedder(
                {
                    initial_text: [1.0, 0.0],
                    updated_text: [1.0, 0.0],
                    question: [1.0, 0.0],
                }
            ),
        )
        self.service.close()
        self.service = HearthService(self.database, semantic_index=index)

        self.service.import_document(str(self.note))
        self.note.write_text(updated_text, encoding="utf-8")
        self.service.reindex_document(str(self.note))
        answer = self.service.answer(question)

        versions = [path for path in (self.root / "semantic-index" / "versions").iterdir() if path.is_dir()]
        self.assertEqual(answer.status, "supported")
        self.assertIn("Lin", answer.text)
        self.assertNotIn("Ada", answer.text)
        self.assertEqual(len(versions), 1)

        self.assertTrue(self.service.remove_document(str(self.note)))
        active_version = json.loads(
            (self.root / "semantic-index" / "active.json").read_text(encoding="utf-8")
        )["version"]
        active_manifest = json.loads(
            (self.root / "semantic-index" / "versions" / active_version / "manifest.json").read_text(encoding="utf-8")
        )
        remaining_versions = [path for path in (self.root / "semantic-index" / "versions").iterdir() if path.is_dir()]

        self.assertEqual(self.service.answer(question).status, "abstained")
        self.assertEqual(active_manifest["chunk_ids"], [])
        self.assertEqual(len(remaining_versions), 1)


if __name__ == "__main__":
    unittest.main()
