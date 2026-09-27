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
| `hearth task new <project> "<goal>"` | Run one agent on the goal in a new worktree, with receipts; `--attach FILE` adds files or images, and `--issue N` starts from a GitHub issue. |
| `hearth task list` | List tasks, newest first. |
| `hearth task show <id>` | Show a task and the commands to review and publish it. |
| `hearth task discard <id>` | Preview, or with `--apply` remove, a task's worktree and branch. |
| `hearth task new <project> "<goal>" --interactive` | Open the agent in a tmux window instead, with the goal as its first message. |
| `hearth task collect <id>` | After an interactive task, commit its checkpoint, run the check, and save the diff. |
| `hearth task open <id>` | Open a task's worktree in VS Code. |
| `hearth open <project>` | Open or attach to the project's tmux session: an editor, a live task list, and interactive tasks. |

## Documentation

Design documents stay on the author's Mac for now, so these links work only in a local checkout:
[knowledge details](docs/knowledge.md), [privacy](docs/privacy.md), [architecture](docs/architecture.md), [decisions](docs/decisions/README.md), [workbench specification](docs/workbench-spec.md), [workbench plan](docs/workbench-plan.md), and [coding requirements](CODING_REQUIREMENTS.md).
