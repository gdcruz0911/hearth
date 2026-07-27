from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hearth.domain import ExtractedPage, SourceDocument
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


if __name__ == "__main__":
    unittest.main()
