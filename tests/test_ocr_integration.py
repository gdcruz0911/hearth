from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from hearth.extraction import PopplerPdfExtractor
from hearth.service import HearthService


@unittest.skipUnless(
    os.environ.get("HEARTH_RUN_OCR_INTEGRATION") == "1",
    "Set HEARTH_RUN_OCR_INTEGRATION=1 to run the local OCRmyPDF integration test.",
)
class OCRIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("ocrmypdf") is None:
            self.skipTest("OCRmyPDF is not installed.")
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.fixture = Path(__file__).parent / "fixtures" / "public" / "ocr-image-only.pdf"
        self.service = HearthService(
            self.root / "hearth.sqlite",
            ocr_output_directory=self.root / "ocr-output",
        )

    def tearDown(self) -> None:
        self.service.close()
        self.temporary_directory.cleanup()

    def test_image_only_pdf_uses_local_ocr_and_preserves_citation_metadata(self) -> None:
        native_document = PopplerPdfExtractor().extract(self.fixture)
        self.assertEqual(native_document.pages[0].text.strip(), "")

        self.service.import_document(str(self.fixture))
        document_id = self.service.reindex_document(str(self.fixture))
        documents = self.service.list_documents()
        inspection = self.service.inspect_document(document_id)
        answer = self.service.answer("Where is the archive?")

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].id, document_id)
        self.assertEqual(documents[0].name, "ocr-image-only.pdf")
        self.assertEqual(documents[0].page_count, 1)
        self.assertEqual(documents[0].chunk_count, 1)
        self.assertEqual(documents[0].ocr_page_count, 1)
        self.assertIsNotNone(inspection)
        assert inspection is not None
        self.assertEqual(inspection.pages[0].extraction_method, "ocr")
        self.assertEqual(len(inspection.pages[0].chunks), 1)
        self.assertEqual(answer.status, "supported")
        self.assertEqual(answer.citations[0].document_name, "ocr-image-only.pdf")
        self.assertEqual(answer.citations[0].page_number, 1)
        self.assertEqual(answer.citations[0].extraction_method, "ocr")
        self.assertIn("cedar shelf", answer.citations[0].quote.lower())
