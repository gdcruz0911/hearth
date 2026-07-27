from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Protocol

from .domain import ExtractedPage, ImportError, SourceDocument


class OCRFallback(Protocol):
    """Local OCR extension point for image-only PDF pages."""

    def extract_page(self, pdf_path: Path, page_number: int) -> ExtractedPage: ...


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

    def __init__(self, native_extractor: PageExtractor | None = None, ocr_fallback: OCRFallback | None = None):
        self._native_extractor = native_extractor
        self._ocr_fallback = ocr_fallback

    def extract(self, path: Path) -> SourceDocument:
        if path.suffix.lower() != ".pdf":
            raise ImportError("PDF extractor only accepts .pdf files.")
        if self._native_extractor is None:
            raise ImportError(
                "PDF extraction is not configured. Connect a local PageExtractor and optional OCRFallback."
            )
        document = self._native_extractor.extract(path)
        if not document.pages:
            raise ImportError("PDF extractor returned no pages.")
        pages = []
        for page in document.pages:
            if page.text.strip() or self._ocr_fallback is None:
                pages.append(page)
            else:
                pages.append(self._ocr_fallback.extract_page(path, page.page_number))
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
