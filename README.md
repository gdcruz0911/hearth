# Hearth

Hearth is a local-first, evidence-bound document chat foundation.
It is designed to answer from imported local documents with page-level source evidence or to abstain when no retrieved evidence supports an answer.

## Status

Hearth currently supports UTF-8 text and Markdown notes, plus PDF text extraction through local Poppler tools.
It stores document, page, section, chunk, and extraction provenance in local SQLite.
It supports local import, search, removal, reindexing, collection inspection, deterministic evaluation, and optional OCRmyPDF fallback for image-only PDF pages.
The default answer path returns retrieved source text with citations instead of generated prose.

The local MLX runtime and three candidate models are provisioned separately for host-side evaluation.
An opt-in MLX generator adapter is implemented behind `--generator-model`.
An opt-in MLX embedding and flat-index path is implemented behind `--embedding-model` and `--index-directory`.
An opt-in MLX reranker is implemented behind `--reranker-model`.
The combined generator and reranker path passed a narrow four-case public host evaluation under the current verbatim-answer contract.
An experimental local claim-support evaluation is available behind `evaluate-claim-support`; its expanded adversarial corpus still does not meet a release threshold.
It remains experimental and is not a product default.

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
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite list
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite inspect 1
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite evaluate tests/fixtures/public/baseline-evaluation.json
```

`list` reports imported document metadata and the ID used by `inspect`.
`inspect` reports page, chunk, extraction, and OCR-warning metadata without rendering stored document text or canonical source paths.

OCR fallback uses `--ocr-output-directory` as private local scratch space and deletes each derived OCR PDF after text extraction by default.
To retain an OCR-enhanced PDF for manual local inspection, pass `--retain-ocr-output` with that directory.

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

- The default retrieval path uses a deterministic hashed-vector scaffold.
- The optional local semantic embedding index is not yet a production-qualified default.
- The optional local reranker is experimental and has not been selected as a default.
- The experimental local claim-support checker is evaluation-only and does not relax the verbatim cited-evidence requirement.
- Generated answers require an explicitly configured local model and remain experimental despite the current narrow citation and abstention evaluation.
- Generated factual text must be a verbatim cited evidence span, so valid paraphrases and multi-source synthesis can abstain.
- OCR quality depends on the source scan and can be poor for handwriting, complex layouts, tables, formulas, and low-quality images.
- OCR-derived PDFs are transient by default; explicitly retained files remain private runtime data and must be managed manually.
- Hearth has no conversational memory.
  It answers from the currently imported collection, so removed documents and reindexed source changes are not retained as answerable evidence.
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
