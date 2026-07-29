from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hearth.domain import ImportError
from hearth.extraction import OCRmyPDFFallback


class UnusedNativeExtractor:
    def extract(self, path: Path):
        raise AssertionError("The native extractor must not run after OCRmyPDF fails.")


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
