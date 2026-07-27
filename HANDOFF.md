# Hearth handoff

## Current state

Hearth is a local-first, evidence-bound document chat foundation on the `master` branch.
The source repository contains code, ADRs, documentation, tests, and synthetic or public fixtures only.
Private documents, OCR output, SQLite databases, model weights, indexes, prompts, and responses must remain outside Git.

The current CLI imports UTF-8 notes and Markdown, extracts PDFs with local Poppler, optionally OCRs blank PDF pages with local OCRmyPDF, stores provenance in SQLite, retrieves with a deterministic hashed-vector scaffold, and either returns direct evidence or abstains.
An opt-in MLX generator adapter accepts a pre-provisioned local model directory and validates strict JSON citation IDs against the request’s retrieved evidence.

## Decisions

Read [the ADR index](docs/decisions/README.md) before changing persistence, OCR, retrieval, local inference, or answer validation.
ADR-0006 requires every generated answer to cite only approved evidence chunk IDs.
ADR-0007 defines the strict JSON generator response protocol and fail-closed abstention behavior.

## Verification

Run the unit suite.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Run the public deterministic evaluation corpus.

```bash
PYTHONPATH=src .venv/bin/python -m hearth.cli --database /private/tmp/hearth-evaluation.sqlite evaluate tests/fixtures/public/baseline-evaluation.json
```

The optional generator requires an interactive macOS terminal with MLX GPU access and a separately provisioned local model.
Do not attempt GPU inference in the Codex sandbox because it has no usable Metal device.

## Immediate next work

Run the opt-in MLX generator against synthetic or public evidence cases from a normal macOS terminal.
Evaluate citation ID validity, supported-answer quality, and unsupported-question abstention before selecting any model as the product default.
Implement local embeddings and reranking only after recording any additional material decision as an ADR.

## Working rules

Make the smallest complete change and preserve unrelated work.
Do not add cloud fallback, telemetry, document uploads, or private fixtures.
Commit only coherent verified milestones and include only task-related files.
