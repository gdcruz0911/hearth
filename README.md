# Hearth

Hearth is a local-first, evidence-bound document chat foundation.
It does not make network requests, download models, send telemetry, or fall back to cloud services.

## First milestone

The current CLI imports UTF-8 text and Markdown notes, plus PDFs through a per-page extraction interface.
It persists document, page, section, and chunk provenance in SQLite.
It uses a deterministic local hashed vector index as a scaffold for an MLX embedding index.
It answers only when retrieved evidence supports the question and otherwise abstains.

OCR, PDF text extraction, vector embedding, reranking, and generation are all explicit interfaces.
The initial implementation includes no hidden approximations of those capabilities.

## Quick start

```bash
python -m hearth.cli --database .hearth/hearth.sqlite import path/to/note.md
python -m hearth.cli --database .hearth/hearth.sqlite search "your question"
python -m unittest discover -s tests -v
```

PDF import uses local Poppler tools when `pdfinfo` and `pdftotext` are installed.
If they are unavailable, the CLI reports the missing local prerequisite instead of pretending that a PDF was imported.
Pass `--ocr-output-directory` to enable the local OCRmyPDF fallback for image-only pages.
OCR output is derived private data and must remain outside version control.

## Planned integrations

- `OcrmyPdfFallback` runs OCRmyPDF locally when an OCR output directory is configured.
- `EmbeddingIndex` can replace the local lexical index with an MLX-backed vector index.
- `Reranker` can connect a local MLX reranker.
- `EvidenceAnswerer` can connect a local MLX generator while preserving evidence identifiers.
