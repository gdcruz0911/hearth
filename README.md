# Hearth

Hearth is being built as a personal agentic organ and workbench on one Mac.
Its intended experience is one persistent place to work directly with several CLI agents and models, delegate tasks, inspect results, and carry useful knowledge into later work.
The keeper chose direct agent sessions first, then delegation and memory.
The current app has task execution, checking, knowledge retrieval, and an Electron dashboard; the complete desktop session workflow is still to be built.

Hearth serves one person, its keeper; it is not a product or a service.

## Current foundations

- **Runs agents in their own space.** Claude and Codex work through their official CLIs, with coding tasks in separate worktrees.
- **Checks delegated coding work.** The task loop records project checks, verification, and review, with providers, models, and effort configurable by role.
  A role can use the same provider or model as another; the record shows who checked what.
- **Answers from your notes.** It finds the answer in your own documents and shows where it came from, or says it found nothing.
- **Keeps you in charge.** Merging and memory promotion require you.
  Delegated publication follows the project's recorded permissions and applicable approvals.
- **Shows task activity.** The local dashboard, also hosted in the Electron app, shows projects, tasks, live transcripts, changes, and knowledge.
  Its current task actions are cancellation, answering questions, and approving tests.

## Privacy

Hearth runs on your Mac and has no accounts, tracking, or error reports of its own.

- **Your notes** stay on your Mac. When a task asks for context, only short excerpts from the folders you allow go to Claude or Codex, and Hearth saves a copy of everything it sends.
- **Your code** goes only to the agents you allow for that project.
- **Your Google account:** `hearth google connect` signs in to one account you choose, with permission only to read mail and to manage a calendar Hearth creates for itself. It cannot send, change, or delete email, or touch your other calendars, and the sign-in stays in your Mac's Keychain.
- **Your mail:** once the morning brief is turned on, mail it reads goes only to the agent that writes the brief and is deleted from your Mac when that run ends. A deadline becomes a calendar event only after Hearth's own checks.
- What Claude or Codex receives falls under the terms of your plan with that company.
- Nothing private, such as your documents, prompts, or passwords, is ever committed to this repository.

## Status

The CLI and backend provide task execution and a checking loop toward reviewable changes or draft pull requests, search and scoped recall, attributed note promotion, and Google sign-in support.
The desktop shell is installed locally, but packaging and task monitoring alone do not make it a usable agent workbench.

Next: create and continue direct sessions from the app, choose CLI/model/effort visibly, work with two agents, send follow-ups, inspect results, and recover conversations and drafts after restart.
Then add conversation-led delegation and stronger memory continuity.
Phone alerts, the morning brief, more widgets, and graph refinements follow that usable workflow.

The acceptance test is a real working session with two CLI agents inside the installed app without a separate terminal for agent orchestration.
The [product direction](PRODUCT.md), [UI specification](docs/workbench-ui.md), and [next implementation plan](docs/workbench-next-plan.md) describe that target and distinguish it from current behavior.

## Setup

Hearth needs a Mac with Python 3.11 or later, and Poppler to read PDFs.
OCRmyPDF, for scanned pages, and MLX, for local models on Apple Silicon, are optional.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Check every change with:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

## Commands

Every command is listed in [docs/commands.md](docs/commands.md), and `hearth <command> --help` shows its options.

## Documentation

Design documents are kept on the author's Mac for now, so these links work only in a local checkout:
[product direction](PRODUCT.md), [vision](VISION.md), [UI specification](docs/workbench-ui.md), [next implementation plan](docs/workbench-next-plan.md), [knowledge details](docs/knowledge.md), [privacy](docs/privacy.md), [architecture](docs/architecture.md), [decisions](docs/decisions/README.md), [original workbench specification](docs/workbench-spec.md), [original workbench plan](docs/workbench-plan.md), and [coding requirements](CODING_REQUIREMENTS.md).
