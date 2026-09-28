from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from hearth.domain import Evidence, ExtractedPage, ImportError, SourceDocument
from hearth.embedding import EmbeddingSpec, FlatVectorIndex
from hearth.extraction import PdfExtractor
from hearth.service import HearthService



def setUpModule() -> None:
    # Index builds read this machine's memory pressure and take a user-wide lock; tests get a steady normal
    # reading and a private lock so they pass whatever else the Mac is doing. Pressure tests patch the reading again.
    lock_directory = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(lock_directory.cleanup)
    for patcher in (
        mock.patch("hearth.embedding._memory_signals", return_value=(1, 0.0)),
        mock.patch("hearth.embedding._BUILD_LOCK_PATH", Path(lock_directory.name) / "build.lock"),
    ):
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)

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


class MutatingPdfExtractor:
    def extract(self, path: Path) -> SourceDocument:
        path.write_bytes(b"changed during extraction")
        return SourceDocument(path=path, pages=(ExtractedPage(1, "This should not be imported."),))


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

    def rebuild(self, chunks, *, on_progress=None, is_cancelled=None, on_warning=None) -> None:
        self.rebuild_chunk_ids.append(tuple(chunk.id for chunk in chunks))
        if on_progress is not None:
            on_progress(len(chunks), len(chunks))

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

    def test_import_fails_when_source_changes_during_extraction(self) -> None:
        pdf = self.root / "changing.pdf"
        pdf.write_bytes(b"initial")
        self.service.close()
        self.service = HearthService(self.database, pdf_extractor=MutatingPdfExtractor())

        with self.assertRaisesRegex(ImportError, "changed during import"):
            self.service.import_document(str(pdf))

        self.assertEqual(self.service.list_documents(), [])

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

    def test_retaining_ocr_output_requires_an_output_directory(self) -> None:
        with self.assertRaisesRegex(ValueError, "retain_ocr_output requires ocr_output_directory"):
            HearthService(self.database, retain_ocr_output=True)

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

    def test_connected_source_roots_preview_supported_files_then_imports_the_approved_plan(self) -> None:
        desktop = self.root / "Desktop"
        documents = self.root / "Documents"
        downloads = self.root / "Downloads"
        for folder in (desktop, documents, downloads):
            folder.mkdir()
        first_note = desktop / "project.md"
        first_note.write_text("# Project\n\nThe project owner is Ada.\n", encoding="utf-8")
        second_note = documents / "readme.txt"
        second_note.write_text("The review is scheduled for Friday.\n", encoding="utf-8")
        (downloads / "photo.jpg").write_bytes(b"not a supported source")
        (desktop / ".private.md").write_text("This file is not scanned.\n", encoding="utf-8")

        plan = self.service.plan_source_import((desktop, documents, downloads))
        result = self.service.import_source_plan(plan)

        self.assertEqual([root.name for root in plan.roots], ["Desktop", "Documents", "Downloads"])
        self.assertEqual([candidate.path.name for candidate in plan.candidates], ["project.md", "readme.txt"])
        self.assertEqual([summary.document.name for summary in result.imported], ["project.md", "readme.txt"])
        self.assertEqual(result.failures, ())
        self.assertEqual([document.name for document in self.service.list_documents()], ["project.md", "readme.txt"])

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

    def test_flat_semantic_index_exposes_explainable_cross_document_relationships(self) -> None:
        operations_text = "The deployment review includes a rollback plan."
        procurement_text = "The release review requires a rollback plan from suppliers."
        operations = self.root / "operations.md"
        procurement = self.root / "procurement.md"
        operations.write_text(operations_text, encoding="utf-8")
        procurement.write_text(procurement_text, encoding="utf-8")
        index = FlatVectorIndex(
            self.root / "semantic-index",
            FakeFlatEmbedder(
                {
                    operations_text: [1.0, 0.0],
                    procurement_text: [0.9, 0.435889894],
                }
            ),
        )
        self.service.close()
        self.service = HearthService(self.database, semantic_index=index)

        operations_id = self.service.import_document(str(operations))
        procurement_id = self.service.import_document(str(procurement))
        relationships = self.service.document_relationships()

        self.assertEqual(len(relationships), 1)
        relationship = relationships[0]
        self.assertEqual((relationship.left_chunk.document_id, relationship.right_chunk.document_id), (operations_id, procurement_id))
        self.assertGreaterEqual(relationship.score, 0.89)
        self.assertIn("rollback plan", relationship.left_chunk.text)
        self.assertIn("rollback plan", relationship.right_chunk.text)

        self.service.close()
        self.service = HearthService(self.database, semantic_index=index, relationship_minimum_score=0.95)
        self.assertEqual(self.service.document_relationships(), [])


if __name__ == "__main__":
    unittest.main()


class StaleChunkingAttentionTests(unittest.TestCase):
    """ADR-0018: a chunking change is reported per document, never silently applied."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "hearth.sqlite"
        self.source = self.root / "note.md"
        self.source.write_text("# Title\n\nAlpha beta gamma. Delta epsilon.\n", encoding="utf-8")
        self.service = HearthService(self.database)
        self.service.import_document(str(self.source))

    def tearDown(self) -> None:
        self.service.close()
        self.temporary_directory.cleanup()

    def _mark_legacy(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE documents SET chunking_version = NULL")
        connection.commit()
        connection.close()

    def test_fresh_import_is_not_stale(self):
        health = self.service.collection_health()
        self.assertEqual(health.stale_chunking_count, 0)
        self.assertEqual(health.source_attention, ())

    def test_legacy_document_is_reported_and_reindex_clears_it(self):
        self._mark_legacy()
        health = self.service.collection_health()
        self.assertEqual(health.stale_chunking_count, 1)
        self.assertEqual([item.status for item in health.source_attention], ["chunking outdated"])

        self.service.reindex_document(str(self.source))
        self.assertEqual(self.service.collection_health().stale_chunking_count, 0)

    def test_unavailable_source_outranks_stale_chunking(self):
        # A missing source cannot be reindexed, so that is the actionable status.
        self._mark_legacy()
        self.source.unlink()
        statuses = [item.status for item in self.service.collection_health().source_attention]
        self.assertEqual(statuses, ["source unavailable"])
