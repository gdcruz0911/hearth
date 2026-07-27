# Hearth

Hearth is a local-first, evidence-bound document chat foundation.
It is designed to answer from imported local documents with page-level source evidence or to abstain when no retrieved evidence supports an answer.

## Status

Hearth currently supports UTF-8 text and Markdown notes, plus PDF text extraction through local Poppler tools.
It stores document, page, section, chunk, and extraction provenance in local SQLite.
It supports local import, search, removal, reindexing, deterministic evaluation, and optional OCRmyPDF fallback for image-only PDF pages.
The current answer path returns retrieved source text with citations instead of generated prose.

The local MLX runtime and three candidate models are provisioned separately for host-side benchmarking.
MLX embeddings, reranking, and generator adapters are not implemented yet.

## Privacy boundary

Hearth keeps imported documents and runtime data on the local machine.
The current application code has no configured cloud service, telemetry, or cloud-model fallback.
Installing dependencies and provisioning models are separate operations that can use package or model registries.
Do not commit private documents, OCR output, SQLite databases, indexes, model weights, prompts, responses, or absolute local paths.

See [privacy guidance](docs/privacy.md) for the precise boundary and Git hygiene.

## Requirements

- Python 3.11 or later.
- Poppler (`pdfinfo` and `pdftotext`) for PDF imports.
- OCRmyPDF for optional OCR of image-only PDF pages.
- macOS on Apple Silicon and MLX for the optional host-side model benchmark.

## Quick start

Create an isolated development environment and install Hearth.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Import a local note, query it, and run the public evaluation corpus.

```bash
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite import path/to/note.md
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite search "your question"
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite evaluate tests/fixtures/public/baseline-evaluation.json
```

## Verification

Run the focused unit suite.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Run the deterministic public evaluation corpus.

```bash
PYTHONPATH=src .venv/bin/python -m hearth.cli --database /private/tmp/hearth-evaluation.sqlite evaluate tests/fixtures/public/baseline-evaluation.json
```

An optional host-side MLX benchmark is documented in [benchmarking](docs/benchmarking.md).

## Limitations

- Retrieval uses a deterministic hashed-vector scaffold rather than a production semantic embedding index.
- Reranking and generated answers are not implemented.
- OCR quality depends on the source scan and can be poor for handwriting, complex layouts, tables, formulas, and low-quality images.
- There is no graphical interface, web server, cloud synchronization, or multi-user support.

## Project documentation

- [Architecture](docs/architecture.md)
- [Privacy](docs/privacy.md)
- [Local inference](docs/local-inference.md)
- [Benchmarking](docs/benchmarking.md)
- [PDF and OCR](docs/pdf-and-ocr.md)
- [Evaluation](docs/evaluation.md)
- [Architecture Decision Records](docs/decisions/README.md)
- [Next milestones](docs/next-milestones.md)
