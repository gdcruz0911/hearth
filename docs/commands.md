# Hearth commands

Link the command once with `ln -s "$PWD/.venv/bin/hearth" ~/.local/bin/hearth` from the repository, then run `hearth` from anywhere; `hearth <command> --help` shows every option.
Wherever a command takes a task ID, `last` or a unique ending of the ID also works, such as `hearth task show last`.
A test fails when a command is missing from this table, so it grows with every pull request that adds one.

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
| `hearth task answer <id> "<text>"` | Answer the question an agent left for you; `hearth loop` then continues the task. |
| `hearth task new <project> "<goal>" --tests-first` | Have another model family write failing tests first; the implementer must pass them unchanged. Add `--approve-tests` to read them before implementation starts. |
| `hearth task approve-tests <id>` | Approve a tests-first task's tests and start implementing. |
| `hearth task escape <id> "<text>"` | Record a problem found after the task passed every gate. |
| `hearth task retro <id> --approve N` | Approve escape N's stored proposal by starting it as a tests-first task. |
| `hearth task retro <id>` | Have another model family propose one permanent fix per escape; you approve by running or editing the printed command. |
| `hearth task collect <id>` | After an interactive task, commit its checkpoint, run the check, and save the diff. |
| `hearth task open <id>` | Open a task's worktree in VS Code. |
| `hearth loop <id>` | Review a finished task with another model family and fix it, up to `--rounds` times, until approved. |
| `hearth stats` | Show runs, usable replies, pass rates, time, models, and effort per provider and role, with escapes and plan use; `--json` for the command center. |
| `hearth review-eval <cases> --reviewer NAME` | Score a reviewer on seeded changes, some with planted bugs, and append the results to `~/.hearth/evals/reviews.jsonl`. |
| `hearth open <project>` | Open or attach to the project's tmux session: an editor, a live task list, and interactive tasks. |
