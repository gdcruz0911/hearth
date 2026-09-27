# Hearth

Hearth is a local-first, evidence-bound personal knowledge hub.
It indexes documents you already own, in place, shows how they relate with evidence on every link, and answers searches with page-level source evidence or abstains.

## Status

Hearth currently supports UTF-8 text and Markdown notes, plus PDF text extraction through local Poppler tools.
It stores document, page, section, chunk, and extraction provenance in local SQLite.
It supports local import, search, removal, reindexing, collection health and inspection, deterministic evaluation, and optional OCRmyPDF fallback for image-only PDF pages.
The default answer path returns retrieved source text with citations instead of generated prose.

The local MLX runtime and two candidate models are provisioned separately for host-side evaluation.
An opt-in MLX embedding and flat-index path is implemented behind `--embedding-model` and `--index-directory`.
An opt-in MLX reranker is implemented behind `--reranker-model`.
Hearth Knowledge does not generate prose answers; see [ADR-0022](docs/decisions/ADR-0022-workbench-boundary.md).

The workbench, in `src/hearth/workbench/`, is the second half of Hearth and coordinates coding agents across three subscription plans.
It currently has one command, `hearth usage`, which reads the five-hour and weekly plan limits that Codex and Claude Code already write locally.
`hearth usage statusline`, registered as Claude Code's status line, records Claude's limits whenever they change.

## Privacy boundary

Hearth has two halves with different promises; see [ADR-0022](docs/decisions/ADR-0022-workbench-boundary.md).

Hearth Knowledge keeps imported documents and runtime data on the local machine.
Its code has no configured cloud service, telemetry, or cloud-model fallback, and it never imports the workbench.

The workbench is the only part of Hearth that may reach the network, and only through the provider tools and destinations its ADRs name.
Its only command so far, `hearth usage`, reads local log files and makes no network request.
Installing dependencies and provisioning models are separate operations that can use package or model registries.
Do not commit private documents, OCR output, SQLite databases, indexes, model weights, prompts, responses, or absolute local paths.

See [privacy guidance](docs/privacy.md) for the precise boundary and Git hygiene.

## Requirements

- Python 3.11 or later.
- Poppler (`pdfinfo` and `pdftotext`) for PDF imports.
- OCRmyPDF for optional OCR of image-only PDF pages.
- macOS on Apple Silicon and MLX for the optional local embedding and reranker models.

## Quick start

Create an isolated development environment and install Hearth.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Import a local note, query it, and run the public evaluation corpus.

```bash
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite import path/to/note.md
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite health
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite search "your question"
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite list
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite inspect 1
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite web
.venv/bin/python -m hearth.cli --database .hearth/hearth.sqlite evaluate tests/fixtures/public/baseline-evaluation.json
```

`list` reports imported document metadata and the ID used by `inspect`.
`inspect` reports page, chunk, extraction, and OCR-warning metadata without rendering stored document text or canonical source paths.
`health` reports collection counts, OCR-review needs, source-file availability and freshness, semantic-index state, and safe next actions without rendering source paths or document text.
When a source changed outside Hearth, `health` asks you to run an explicit `reindex` rather than changing stored evidence automatically.
`import` and `reindex` report extraction counts, semantic-index state, and the configured OCR artifact outcome.
`search` prints the evidence-bound answer followed by its compact source metadata and the cited evidence text.
`sources preview` scans the profile’s connected folders for supported files without extracting text or changing the collection.
`sources import` imports the currently eligible files after that explicit command, while leaving each original file in place.
Hearth never moves, renames, rewrites, or deletes a source file, and source watching is intentionally unsupported.

## Commands

Run these as `.venv/bin/hearth`, or put `.venv/bin` on your `PATH`; `hearth <command> --help` shows every option.
A test fails when a command is missing from this table.

| Command | What it does |
| --- | --- |
| `hearth import <path>` | Import one note or PDF. |
| `hearth search "<question>"` | Answer from cited evidence, or abstain. |
| `hearth list` | List imported documents and their IDs. |
| `hearth inspect <id>` | Show one document's provenance metadata. |
| `hearth health` | Summarize collection attention items and next actions. |
| `hearth reindex <path>` | Re-extract one changed document. |
| `hearth remove <path>` | Remove a document's records; the file stays. |
| `hearth sources preview` | List eligible files in connected folders without importing. |
| `hearth sources import` | Import the eligible files the preview listed. |
| `hearth web` | Open the local web interface on this Mac only. |
| `hearth evaluate <corpus>` | Run a synthetic or public evaluation corpus. |
| `hearth profile create <path>` | Create a private runtime profile. |
| `hearth usage` | Show five-hour and weekly plan use for Claude, Codex, and Antigravity. |
| `hearth usage statusline` | Record Claude's limits from its status line JSON. |
| `hearth task new <project> "<goal>"` | Run one agent on the goal in a new worktree, with receipts. |
| `hearth task list` | List tasks, newest first. |
| `hearth task show <id>` | Show a task and the commands to review and publish it. |
| `hearth task discard <id>` | Preview, or with `--apply` remove, a task's worktree and branch. |

## Local web interface

`web` opens a visual local interface on `127.0.0.1` and prints a random per-launch local URL.
Use `web --no-open` when you want to copy that URL into a browser manually.
For repeatable local model and index settings, create a private runtime profile with `hearth profile create` and launch with `hearth --profile /path/to/hearth.json web`.
The UI uses a native macOS chooser to import an original file in place, so it does not create a browser-upload copy or managed library.
It provides connected-folder review, health, metadata-only inspection, evidence-bound search, a semantic-neighborhood map when a local embedding index is configured, and preview-then-apply controls for semantic-index rebuild, reindex, and collection-record removal.
The map is an overview of evidence-backed relationships, not a visual list of every imported source.
Removing a collection record never deletes its source file.

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

## Limitations

- The default retrieval path uses a deterministic hashed-vector scaffold.
- The optional local semantic embedding index is not yet a production-qualified default.
- The optional local reranker is experimental and has not been selected as a default.
- Search returns cited source passages, not a written answer or a synthesis across sources.
- OCR quality depends on the source scan and can be poor for handwriting, complex layouts, tables, formulas, and low-quality images.
- OCR-derived PDFs are transient by default; explicitly retained files remain private runtime data and must be managed manually.
- Hearth has no conversational memory.
  It answers from the currently imported collection, so removed documents and reindexed source changes are not retained as answerable evidence.
- `health` detects changes only at an imported file's stored path.
  An externally moved source is reported as unavailable until you restore it, or remove its stale record and import it from its new location.
- The web interface is local-only and single-user.
  It has no cloud synchronization, account system, LAN binding, public deployment, or multi-user support.

## Project documentation

- [Domain glossary](CONTEXT.md)
- [Architecture](docs/architecture.md)
- [Privacy](docs/privacy.md)
- [Local inference](docs/local-inference.md)
- [PDF and OCR](docs/pdf-and-ocr.md)
- [Evaluation](docs/evaluation.md)
- [Architecture Decision Records](docs/decisions/README.md)
- [Workbench specification](docs/workbench-spec.md)
- [Workbench implementation plan](docs/workbench-plan.md), including the knowledge track
- [Coding requirements](CODING_REQUIREMENTS.md)
