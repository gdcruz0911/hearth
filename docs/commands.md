# Hearth commands

Link the command once with `ln -s "$PWD/.venv/bin/hearth" ~/.local/bin/hearth` from the repository, then run `hearth` from anywhere; `hearth <command> --help` shows every option.
Wherever a command takes a task ID, `last` or a unique ending of the ID also works, such as `hearth task show last`.
Agents run on the models in `~/.hearth/models.json`, one per provider, such as `{"claude": "claude-sonnet-5-5", "codex": "gpt-6-sol"}`, for every role; without it Hearth uses its built-in pins, never the apps' own defaults, and `--model` overrides both for one task.
A test fails when a command is missing from this table, so it grows with every pull request that adds one.

| Command | What it does |
| --- | --- |
| `hearth import <path>` | Import one note or PDF. |
| `hearth search "<question>"` | Answer from cited evidence, or abstain. Add `--keyword` to search exact words only, skipping semantic search, and `--json` for one JSON value that separates accepted evidence from retrieved candidates. |
| `hearth ask "<question>"` | Answer from your notes with one tool-less Claude turn, limited to its recall roots; shows the reply only when it cites excerpts, then each cited excerpt verbatim, and otherwise abstains. Add `--keyword` for exact words. The exchange is saved under `~/.hearth/asks/`. |
| `hearth google connect --account <email> --client <file>` | Sign in to your brief Google account in the browser, asking only for read-only mail and a calendar Hearth creates for itself. Refuses a consent that grants more or less, or a different account. Keeps the client and refresh token in the Keychain; `--client` is the Desktop app JSON from Google Cloud, needed the first time. Refused inside a task. |
| `hearth google status` | Show the connected account and scopes without contacting Google. Add `--json` for one JSON value. |
| `hearth google disconnect` | Show what would be deleted; `--apply` deletes Hearth's Google tokens from the Keychain. Remove Hearth from your Google account's connections too. |
| `hearth list` | List imported documents and their IDs. Add `--json` for one JSON array with each document's ID, name, and page, chunk, and OCR page counts, never text or source paths. |
| `hearth inspect <id>` | Show one document's provenance metadata. |
| `hearth health` | Summarize collection attention items and next actions. |
| `hearth reindex <path>` | Re-extract one changed document. |
| `hearth remove <path>` | Remove a document's records; the file stays. |
| `hearth sources preview` | List eligible files in connected folders without importing. |
| `hearth sources import` | Import the eligible files the preview listed. |
| `hearth web` | Open the local web interface on this Mac only: one app with Hub, Tasks, and Knowledge tabs, showing what needs you, what is running, and what finished; choose the tab it opens on in the sidebar. The link it opens works once; reloading the page or opening another browser needs a fresh `hearth web`. |
| `hearth recall "<question>"` | For agents: search only the profile's `recall_roots` and print one JSON value like `search --json`, with the scope named; refuses when no recall roots are set. |
| `hearth evaluate <corpus>` | Run a synthetic or public evaluation corpus. |
| `hearth evaluate-questions <question-set> --out <records.jsonl>` | Record every retrieval stage for a labeled question set against the current collection, scoring only supported and unsupported cases. |
| `hearth profile create <path>` | Create a private runtime profile. |
| `hearth usage` | Show five-hour and weekly plan use for Claude, Codex, and Antigravity. |
| `hearth usage statusline` | Record Claude's limits from its status line JSON. |
| `hearth task new <project> "<goal>"` | Run one agent on the goal in a new worktree, with receipts; `--attach FILE` adds files or images, and `--issue N` starts from a GitHub issue. |
| `hearth task list` | List tasks, newest first. Add `--json` for one object with every task's status, stage, open questions, queue state, failure reason, delivered recall without excerpt text, and artifact paths, plus slot use. |
| `hearth task show <id>` | Show a task and the commands to review and publish it. Add `--json` for the same task object `task list --json` prints. |
| `hearth task cancel <id>` | Stop a task's active run and let no further agent start; the task ends as `cancelled`, and its worktree, commits, and receipts stay. Also on the task's page in `hearth web`, after a preview. Refused inside a task. |
| `hearth task discard <id>` | Preview, or with `--apply` remove, a task's worktree and branch. |
| `hearth task new <project> "<goal>" --interactive` | Open the agent in a tmux window instead, with the goal as its first message. |
| `hearth task answer <id> "<text>"` | Answer the question an agent left for you; `hearth loop` then continues the task. On the task's page in `hearth web`, a previewed answer also continues the task in its own process. Refused inside a task. |
| `hearth task new <project> "<goal>" --tests-first` | Have another model family write failing tests first; the implementer must pass them unchanged. Add `--approve-tests` to read them before implementation starts. |
| `hearth task approve-tests <id>` | Approve a tests-first task's tests and start implementing. The task's page in `hearth web` can approve them too, after a preview, when their diff is shown in full; implementation then runs in its own process. Either way the tests are approved once, only as they were written, and the implementer does not start if they changed after the approval. Refused inside a task. |
| `hearth task publish <id>` | Push an approved task and open or update its draft pull request (DEL-7), in a project with `"pr": true`; `hearth loop` does this on approval. A task that received excerpts from your notes pushes nothing until you have read the exact commits, changes, and pull request text it prints and run the command again with `--approve` and the code printed with them, and it is refused outright when it copies a delivered excerpt. |
| `hearth task promote <id>` | Show the task's final report as it would be written to your notes folder's `reports/`, and each note in its `## Notes for the vault` section as it would be written to `agent-notes/<provider>/`, with which agents could recall them. `--apply` records each file, writes it, and commits only those files with the agent as author; it never overwrites, never imports, and refuses a task whose recalled evidence a reader of `reports/` may not receive. Running it again finishes an interrupted promotion. Move a note into `notes/` to accept it. Refused inside a task. |
| `hearth task ci <id>` | Read the pull request's checks: wait while pending, mark it ready when all pass, or send the failing log to a fix run. Never merges. |
| `hearth task escape <id> "<text>"` | Record a problem found after the task passed every gate. |
| `hearth task retro <id> --approve N` | Approve escape N's stored proposal by starting it as a tests-first task. |
| `hearth task retro <id>` | Have another model family propose one permanent fix per escape; you approve by running or editing the printed command. |
| `hearth task collect <id>` | After an interactive task, commit its checkpoint, run the check, and save the diff. |
| `hearth task open <id>` | Open a task's worktree in VS Code. |
| `hearth loop <id>` | Review a finished task with another model family and fix it, up to `--rounds` times, until approved. |
| `hearth verify-eval <cases> --verifier NAME` | Score a verifier on seeded changes whose stated behavior does or does not hold, and append the results to `~/.hearth/evals/verifies.jsonl`. |
| `hearth stats` | Show runs, usable replies, pass rates, time, models, and effort per provider and role, with escapes and plan use; `--json` for the command center. |
| `hearth review-eval <cases> --reviewer NAME` | Score a reviewer on seeded changes, some with planted bugs, and append the results to `~/.hearth/evals/reviews.jsonl`. |
| `hearth open <project>` | Open or attach to the project's tmux session: an editor, a live task list, and interactive tasks. |
