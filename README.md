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
python -m hearth.cli --database .hearth/hearth.sqlite evaluate tests/fixtures/public/baseline-evaluation.json
python -m unittest discover -s tests -v
```

To install the optional local MLX runtime, create an ignored virtual environment and install the extra.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ".[local-inference]"
```

## Local model benchmark

The first local generator candidate is stored outside the repository at `~/Library/Application Support/Hearth/models/qwen3-8b-4bit`.
Run its offline benchmark from an interactive macOS terminal with Metal access.

```bash
scripts/benchmark_qwen3_8b.sh
```

The runner refuses to use the network and uses only a public smoke-test prompt.
The smoke test is the GPU verification and will fail without a usable local MLX device.
It reports prompt throughput, generation throughput, and peak memory for three trials.
It does not evaluate document retrieval, citations, or abstention.
Those require the future evidence-bound model-adapter integration.

Each downloaded model is isolated in its own directory under `~/Library/Application Support/Hearth/models/`.
To remove a model, move only that model's exact directory to the macOS Trash in Finder.
Do not remove the surrounding `Hearth` directory because it also contains the local model cache and may later contain other runtime data.

PDF import uses local Poppler tools when `pdfinfo` and `pdftotext` are installed.
If they are unavailable, the CLI reports the missing local prerequisite instead of pretending that a PDF was imported.
Pass `--ocr-output-directory` to enable the local OCRmyPDF fallback for image-only pages.
OCR output is derived private data and must remain outside version control.

The evaluation command reports only case IDs and pass or fail status.
Use synthetic or public documents in version-controlled corpora.
Do not add private prompts, responses, or document contents to an evaluation corpus.

## Planned integrations

- `OcrmyPdfFallback` runs OCRmyPDF locally when an OCR output directory is configured.
- `EmbeddingIndex` can replace the local lexical index with an MLX-backed vector index.
- `Reranker` can connect a local MLX reranker.
- `EvidenceAnswerer` can connect a local MLX generator while preserving evidence identifiers.
