# Hearth

Hearth is a personal workbench on one Mac.
You say what you want done, coding agents you already pay for do the work, other agents check it, and Hearth keeps an honest record of all of it.
It also keeps your own notes searchable, so the agents can build on what you already know.

Hearth serves one person, its keeper; it is not a product or a service.

## What it does

- **Runs agents in their own space.** Claude and Codex work through their official apps, each task in a separate copy of the project, so nothing reaches your work until you accept it.
- **Checks the work.** Every change runs the project's tests, a second agent runs the real program to confirm it, and an agent from a different company reviews it. No agent grades its own work.
- **Answers from your notes.** It finds the answer in your own documents and shows where it came from, or says it found nothing.
- **Keeps you in charge.** Merging, publishing, and anything that cannot be undone wait for you, and no agent can act as you.
- **Shows everything in one place.** `hearth web` opens a dashboard on your Mac with your projects, tasks, live agent sessions, and notes.

## Privacy

Hearth runs on your Mac and has no accounts, tracking, or error reports of its own.

- **Your notes** stay on your Mac. When a task asks for context, only short excerpts from the folders you allow go to Claude or Codex, and Hearth saves a copy of everything it sends.
- **Your code** goes only to the agents you allow for that project.
- **Your Google account:** `hearth google connect` signs in to one account you choose, with permission only to read mail and to manage a calendar Hearth creates for itself. It cannot send, change, or delete email, or touch your other calendars, and the sign-in stays in your Mac's Keychain.
- **Your mail:** once the morning brief is turned on, mail it reads goes only to the agent that writes the brief and is deleted from your Mac when that run ends. A deadline becomes a calendar event only after Hearth's own checks.
- What Claude or Codex receives falls under the terms of your plan with that company.
- Nothing private, such as your documents, prompts, or passwords, is ever committed to this repository.

## Status

Working now: tasks from request to reviewed pull request, search and recall from your notes, the dashboard, attributed agent notes in your notes folder, and the Google sign-in.
Next: full task and agent views in the dashboard, phone alerts through a Hearth calendar, and the morning brief.

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
[knowledge details](docs/knowledge.md), [privacy](docs/privacy.md), [architecture](docs/architecture.md), [decisions](docs/decisions/README.md), [workbench specification](docs/workbench-spec.md), [workbench plan](docs/workbench-plan.md), and [coding requirements](CODING_REQUIREMENTS.md).
