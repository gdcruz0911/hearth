# Hearth

Hearth has two halves on one Mac:

- **Hearth Knowledge** indexes documents you already own, in place, and answers searches with cited page-level evidence or abstains.
- **The workbench** runs the coding agents you already pay for (Claude Code, Codex, and Antigravity) in isolated Git worktrees, tracks each plan's usage limits, and keeps a receipt of every run.

## Status

Knowledge: import, search, health, reindex, connected folders, OCR fallback, and a local web interface.
Workbench: plan usage, and agent tasks run headlessly or in tmux, each in its own worktree with a checkpoint, check, and diff.
Next: a cross-model review loop and a task board for agent messages.

## Privacy

Hearth Knowledge never sends documents anywhere; its only network request is a model download you approve.
The workbench sends a project's code only to the agent CLIs that project allows, and saves every prompt before sending it.
Never commit private documents, OCR output, databases, indexes, model weights, prompts, responses, or absolute local paths.

## Setup

Needs Python 3.11 or later, Poppler for PDFs, and optionally OCRmyPDF for scanned pages and MLX on Apple Silicon for local models.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Check every change with:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

## Commands

Every command, one line each, is in [docs/commands.md](docs/commands.md); `hearth <command> --help` shows its options.

## Documentation

Design documents stay on the author's Mac for now, so these links work only in a local checkout:
[knowledge details](docs/knowledge.md), [privacy](docs/privacy.md), [architecture](docs/architecture.md), [decisions](docs/decisions/README.md), [workbench specification](docs/workbench-spec.md), [workbench plan](docs/workbench-plan.md), and [coding requirements](CODING_REQUIREMENTS.md).
