from __future__ import annotations

from pathlib import Path
import re
import subprocess
from uuid import uuid4
from typing import Protocol

from .domain import ExtractedPage, ImportError, SourceDocument


class OCRFallback(Protocol):
    """Local OCR extension point for image-only PDF pages."""

    def extract_pages(self, pdf_path: Path, page_numbers: tuple[int, ...]) -> dict[int, ExtractedPage]: ...


class PageExtractor(Protocol):
    """Extracts text page by page without sending content off-device."""

    def extract(self, path: Path) -> SourceDocument: ...


class TextNoteExtractor:
    """Imports UTF-8 text and Markdown files as one logical page."""

    supported_suffixes = {".md", ".markdown", ".txt"}

    def extract(self, path: Path) -> SourceDocument:
        if path.suffix.lower() not in self.supported_suffixes:
            raise ImportError(f"Unsupported note type: {path.suffix or 'no extension'}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ImportError("Notes must be UTF-8 encoded.") from exc
        if not text.strip():
            raise ImportError("Cannot import an empty note.")
        section = _first_markdown_heading(text)
        return SourceDocument(path=path, pages=(ExtractedPage(1, text, section),))


class PdfExtractor:
    """Boundary for a local PDF extractor with optional local OCR fallback."""

    def __init__(self, native_extractor: PageExtractor, ocr_fallback: OCRFallback | None = None):
        self._native_extractor = native_extractor
        self._ocr_fallback = ocr_fallback

    def extract(self, path: Path) -> SourceDocument:
        if path.suffix.lower() != ".pdf":
            raise ImportError("PDF extractor only accepts .pdf files.")
        document = self._native_extractor.extract(path)
        if not document.pages:
            raise ImportError("PDF extractor returned no pages.")
        blank_page_numbers = tuple(page.page_number for page in document.pages if not page.text.strip())
        ocr_pages = (
            self._ocr_fallback.extract_pages(path, blank_page_numbers)
            if blank_page_numbers and self._ocr_fallback is not None
            else {}
        )
        pages = [ocr_pages.get(page.page_number, page) for page in document.pages]
        return SourceDocument(path=path, pages=tuple(pages))


class PopplerPdfExtractor:
    """Per-page local PDF text extraction through Poppler command-line tools."""

    def extract(self, path: Path) -> SourceDocument:
        try:
            metadata = subprocess.run(
                ["pdfinfo", str(path)], check=True, capture_output=True, text=True, timeout=30
            ).stdout
            page_count = _page_count(metadata)
            pages = tuple(self._extract_page(path, page_number) for page_number in range(1, page_count + 1))
        except FileNotFoundError as exc:
            raise ImportError(
                "PDF import requires local Poppler tools: pdfinfo and pdftotext."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ImportError("PDF extraction timed out after 30 seconds.") from exc
        except subprocess.CalledProcessError as exc:
            raise ImportError("Local PDF extraction failed.") from exc
        return SourceDocument(path=path, pages=pages)

    def _extract_page(self, path: Path, page_number: int) -> ExtractedPage:
        result = subprocess.run(
            ["pdftotext", "-f", str(page_number), "-l", str(page_number), "-layout", str(path), "-"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return ExtractedPage(page_number=page_number, text=result.stdout, extraction_method="native")


class OCRmyPDFFallback:
    """Runs local OCRmyPDF and deletes its derived PDF unless retention is explicitly enabled."""

    def __init__(self, native_extractor: PageExtractor, output_directory: Path, retain_output: bool = False):
        self._native_extractor = native_extractor
        self._output_directory = output_directory
        self._retain_output = retain_output

    def extract_pages(self, pdf_path: Path, page_numbers: tuple[int, ...]) -> dict[int, ExtractedPage]:
        if not page_numbers:
            return {}
        self._output_directory.mkdir(parents=True, exist_ok=True)
        output_path = self._output_directory / f"{pdf_path.stem}-{uuid4().hex}.pdf"
        try:
            subprocess.run(
                ["ocrmypdf", "--skip-text", "--output-type", "pdf", str(pdf_path), str(output_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )
            ocr_document = self._native_extractor.extract(output_path)
            requested = set(page_numbers)
            return {
                page.page_number: ExtractedPage(
                    page_number=page.page_number,
                    text=page.text,
                    section=page.section,
                    extraction_method="ocr",
                    ocr_confidence=None,
                )
                for page in ocr_document.pages
                if page.page_number in requested and page.text.strip()
            }
        except FileNotFoundError as exc:
            raise ImportError("OCR fallback requires local OCRmyPDF.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ImportError("OCR fallback timed out after 10 minutes.") from exc
        except subprocess.CalledProcessError as exc:
            raise ImportError("Local OCR fallback failed.") from exc
        finally:
            if not self._retain_output:
                _delete_temporary_ocr_output(output_path)


def _delete_temporary_ocr_output(output_path: Path) -> None:
    try:
        output_path.unlink(missing_ok=True)
    except OSError as exc:
        raise ImportError(
            "Temporary OCR output could not be deleted. Remove it manually before retrying."
        ) from exc


def _first_markdown_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if heading:
                return heading
    return None


def _page_count(pdfinfo_output: str) -> int:
    match = re.search(r"^Pages:\s+(\d+)\s*$", pdfinfo_output, flags=re.MULTILINE)
    if match is None or int(match.group(1)) < 1:
        raise ImportError("PDF metadata did not include a valid page count.")
    return int(match.group(1))
