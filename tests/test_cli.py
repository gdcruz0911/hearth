from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from hearth.cli import main
from hearth.domain import ExtractedPage, SourceDocument
from hearth.service import HearthService


class OcrPdfExtractor:
    def extract(self, path: Path) -> SourceDocument:
        return SourceDocument(
            path=path,
            pages=(
                ExtractedPage(
                    page_number=1,
                    text="OCR-derived archive metadata.",
                    extraction_method="ocr",
                    ocr_confidence=0.92,
                ),
            ),
        )


class CollectionInspectionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "hearth.sqlite"
        self.note = self.root / "operations.md"
        self.note.write_text("# Operations\n\nThe deployment owner is Ada.\n", encoding="utf-8")
        service = HearthService(self.database)
        self.document_id = service.import_document(str(self.note))
        service.close()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_list_reports_metadata_without_document_text_or_source_path(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "list"])

        self.assertEqual(exit_code, 0)
        self.assertIn(f"{self.document_id}: operations.md", output.getvalue())
        self.assertIn("pages: 1, chunks: 1, OCR pages: 0", output.getvalue())
        self.assertNotIn("The deployment owner is Ada.", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_health_reports_collection_metadata_and_next_action_without_document_text(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "health"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Collection health", output.getvalue())
        self.assertIn("Documents: 1", output.getvalue())
        self.assertIn("Pages: 1", output.getvalue())
        self.assertIn("Chunks: 1", output.getvalue())
        self.assertIn("OCR pages needing review: 0", output.getvalue())
        self.assertIn("Source files unavailable: 0", output.getvalue())
        self.assertIn("Sources changed since import: 0", output.getvalue())
        self.assertIn("Sources requiring baseline reindex: 0", output.getvalue())
        self.assertIn("Semantic index: not configured", output.getvalue())
        self.assertIn("Next: import a document, or search the current collection.", output.getvalue())
        self.assertNotIn("The deployment owner is Ada.", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_health_reports_unavailable_source_without_rendering_its_path(self) -> None:
        self.note.rename(self.root / "operations-moved.md")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "health"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Source files unavailable: 1", output.getvalue())
        self.assertIn("Needs attention", output.getvalue())
        self.assertIn("Document 1: operations.md - source unavailable", output.getvalue())
        self.assertIn("Next: restore the source file, or remove its stale collection record.", output.getvalue())
        self.assertNotIn("operations-moved.md", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_import_reports_extraction_and_derived_artifact_status(self) -> None:
        second_note = self.root / "security.md"
        second_note.write_text("# Security\n\nThe archive is local.\n", encoding="utf-8")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "import", str(second_note)])

        self.assertEqual(exit_code, 0)
        self.assertIn("Imported document", output.getvalue())
        self.assertIn("security.md", output.getvalue())
        self.assertIn("Extracted: 1 pages, 1 chunks, 0 OCR pages.", output.getvalue())
        self.assertIn("Semantic index: not configured.", output.getvalue())
        self.assertIn("OCR artifacts: not used.", output.getvalue())

    def test_search_uses_citation_first_output(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "search", "Who is the deployment owner?"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Answer\n# Operations", output.getvalue())
        self.assertIn("The deployment owner is Ada.", output.getvalue())
        self.assertIn("Sources\n- operations.md, page 1, section Operations", output.getvalue())
        self.assertIn("Evidence: # Operations", output.getvalue())
        self.assertIn("The deployment owner is Ada.", output.getvalue())

    def test_search_marks_ocr_citations_for_review(self) -> None:
        pdf = self.root / "scanned.pdf"
        pdf.write_bytes(b"placeholder")
        service = HearthService(self.database, pdf_extractor=OcrPdfExtractor())
        service.import_document(str(pdf))
        service.close()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "search", "What archive metadata is available?"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Sources", output.getvalue())
        self.assertIn("scanned.pdf, page 1", output.getvalue())
        self.assertIn("OCR warning: verify against the original document (confidence: 0.92).", output.getvalue())

    def test_inspect_reports_page_and_chunk_metadata_without_document_text(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "inspect", str(self.document_id)])

        self.assertEqual(exit_code, 0)
        self.assertIn(f"Document {self.document_id}: operations.md", output.getvalue())
        self.assertIn("Page 1 (section: Operations, extraction: native, chunks: 1)", output.getvalue())
        self.assertRegex(output.getvalue(), r"Chunk \d+ \(characters: 0-\d+\)")
        self.assertNotIn("The deployment owner is Ada.", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_inspect_returns_failure_for_unknown_document(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "inspect", "999"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(output.getvalue(), "No imported document with ID 999.\n")

    def test_inspect_marks_ocr_pages_for_review_without_rendering_text(self) -> None:
        pdf = self.root / "scanned.pdf"
        pdf.write_bytes(b"placeholder")
        service = HearthService(self.database, pdf_extractor=OcrPdfExtractor())
        document_id = service.import_document(str(pdf))
        service.close()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "inspect", str(document_id)])

        self.assertEqual(exit_code, 0)
        self.assertIn("extraction: ocr", output.getvalue())
        self.assertIn("OCR confidence: 0.92", output.getvalue())
        self.assertIn("OCR warning: verify against the original document", output.getvalue())
        self.assertNotIn("OCR-derived archive metadata.", output.getvalue())

    def test_retain_ocr_output_requires_an_output_directory(self) -> None:
        error = io.StringIO()

        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as exit_context:
            main(["--retain-ocr-output", "list"])

        self.assertEqual(exit_context.exception.code, 2)
        self.assertIn("--retain-ocr-output requires --ocr-output-directory.", error.getvalue())


if __name__ == "__main__":
    unittest.main()
