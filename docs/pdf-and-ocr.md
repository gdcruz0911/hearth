# PDF and OCR

## Native PDF extraction

Hearth imports PDFs one page at a time through local Poppler commands.
Digital PDFs use `pdfinfo` to determine page count and `pdftotext` to extract page text.
The import fails clearly when the local Poppler tools are unavailable or extraction fails.

## OCR fallback

OCR is optional and enabled by passing `--ocr-output-directory` to the CLI.
`OCRmyPDFFallback` runs local OCRmyPDF once for a PDF only when native extraction produced blank pages.
It substitutes OCR text only for those blank pages and preserves native text for pages that already had usable extraction.
Pages produced by the fallback are marked with the `ocr` extraction method in citations.

The output directory is caller-configured private scratch space because OCR PDFs are derived private data.
Hearth deletes the derived OCR PDF after local text extraction by default.
If manual inspection needs the enhanced PDF, explicitly pass `--retain-ocr-output` with the output directory.
Retained files are private runtime data and remain in that caller-managed directory until manually removed.
Hearth does not offer a broad cleanup command because it cannot safely delete unrelated files from a caller-owned directory.
OCR failures and timeouts are reported as import errors rather than silently returning incomplete extraction.

## Quality limits

OCR quality depends on scan quality and layout.
Handwriting, dense tables, formulas, multi-column layouts, unusual fonts, low-resolution images, skew, and poor contrast can yield inaccurate text.
Use the cited page and quote to inspect important answers against the original document.
Native text extraction is preferred whenever it is available.

## Local prerequisites

Install Poppler for digital PDF extraction.
Install OCRmyPDF for the optional fallback.
Both tools run locally when Hearth invokes them.

## Decision record

The ingestion and artifact-retention policies are defined in [ADR-0003](decisions/ADR-0003-local-pdf-and-ocr-ingestion.md) and [ADR-0011](decisions/ADR-0011-transient-ocr-artifact-lifecycle.md).
