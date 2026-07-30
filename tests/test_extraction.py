from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hearth.domain import ExtractedPage, ImportError, SourceDocument
from hearth.extraction import OCRmyPDFFallback


class UnusedNativeExtractor:
    def extract(self, path: Path):
        raise AssertionError("The native extractor must not run after OCRmyPDF fails.")


class OcrNativeExtractor:
    def extract(self, path: Path) -> SourceDocument:
        return SourceDocument(path, (ExtractedPage(1, "OCR text"),))


class OCRmyPDFFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"placeholder")
        self.output_directory = self.root / "ocr-output"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_ocr_failure_removes_partial_output_and_reports_import_error(self) -> None:
        fallback = OCRmyPDFFallback(UnusedNativeExtractor(), self.output_directory)

        def partial_output_then_failure(command, **kwargs):
            Path(command[-1]).write_bytes(b"partial OCR output")
            raise subprocess.CalledProcessError(1, command)

        with patch("hearth.extraction.subprocess.run", side_effect=partial_output_then_failure):
            with self.assertRaisesRegex(ImportError, "Local OCR fallback failed"):
                fallback.extract_pages(self.pdf, (1,))

        self.assertEqual(list(self.output_directory.iterdir()), [])

    def test_successful_ocr_deletes_temporary_output_by_default(self) -> None:
        fallback = OCRmyPDFFallback(OcrNativeExtractor(), self.output_directory)

        def successful_ocr(command, **kwargs):
            Path(command[-1]).write_bytes(b"OCR output")
            return subprocess.CompletedProcess(command, 0)

        with patch("hearth.extraction.subprocess.run", side_effect=successful_ocr):
            pages = fallback.extract_pages(self.pdf, (1,))

        self.assertEqual(pages[1].text, "OCR text")
        self.assertEqual(list(self.output_directory.iterdir()), [])

    def test_explicit_retention_keeps_ocr_output_for_inspection(self) -> None:
        fallback = OCRmyPDFFallback(OcrNativeExtractor(), self.output_directory, retain_output=True)

        def successful_ocr(command, **kwargs):
            Path(command[-1]).write_bytes(b"OCR output")
            return subprocess.CompletedProcess(command, 0)

        with patch("hearth.extraction.subprocess.run", side_effect=successful_ocr):
            fallback.extract_pages(self.pdf, (1,))

        retained_files = list(self.output_directory.iterdir())
        self.assertEqual(len(retained_files), 1)
        self.assertEqual(retained_files[0].read_bytes(), b"OCR output")

    def test_cleanup_failure_reports_import_error(self) -> None:
        fallback = OCRmyPDFFallback(OcrNativeExtractor(), self.output_directory)

        def successful_ocr(command, **kwargs):
            Path(command[-1]).write_bytes(b"OCR output")
            return subprocess.CompletedProcess(command, 0)

        with (
            patch("hearth.extraction.subprocess.run", side_effect=successful_ocr),
            patch("hearth.extraction.Path.unlink", side_effect=OSError("permission denied")),
            self.assertRaisesRegex(ImportError, "Temporary OCR output could not be deleted"),
        ):
            fallback.extract_pages(self.pdf, (1,))
