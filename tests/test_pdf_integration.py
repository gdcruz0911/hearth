from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hearth.extraction import PopplerPdfExtractor
from hearth.service import HearthService


@unittest.skipUnless(
    shutil.which("pdfinfo") is not None and shutil.which("pdftotext") is not None,
    "Poppler is required for the native PDF integration test.",
)
class NativePdfIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.fixture = Path(__file__).parent / "fixtures" / "public" / "native-text-two-page.pdf"
        self.service = HearthService(self.root / "hearth.sqlite")

    def tearDown(self) -> None:
        self.service.close()
        self.temporary_directory.cleanup()

    def test_native_pdf_preserves_page_boundaries_and_citations(self) -> None:
        document = PopplerPdfExtractor().extract(self.fixture)

        self.assertEqual([page.page_number for page in document.pages], [1, 2])
        self.assertEqual([page.extraction_method for page in document.pages], ["native", "native"])
        self.assertIn("The project review is on Tuesday at 10:00 AM.", document.pages[0].text)
        self.assertNotIn("recovery archive", document.pages[0].text)
        self.assertIn("The recovery archive is stored in encrypted local storage.", document.pages[1].text)
        self.assertNotIn("project review", document.pages[1].text)

        document_id = self.service.import_document(str(self.fixture))
        inspection = self.service.inspect_document(document_id)
        review_answer = self.service.answer("When is the project review?")
        archive_answer = self.service.answer("Where is the recovery archive stored?")

        self.assertIsNotNone(inspection)
        assert inspection is not None
        self.assertEqual(inspection.document.page_count, 2)
        self.assertEqual(inspection.document.ocr_page_count, 0)
        self.assertEqual([page.extraction_method for page in inspection.pages], ["native", "native"])
        self.assertEqual(review_answer.status, "supported")
        self.assertEqual(review_answer.citations[0].page_number, 1)
        self.assertIn("Tuesday", review_answer.citations[0].quote)
        self.assertEqual(archive_answer.status, "supported")
        self.assertEqual(archive_answer.citations[0].page_number, 2)
        self.assertIn("encrypted local storage", archive_answer.citations[0].quote)
