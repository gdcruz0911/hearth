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


if __name__ == "__main__":
    unittest.main()
