"""A stand-in provider CLI for workbench tests.

It reads the prompt on standard input and prints Claude-format stream events, or Codex-format ones after `--as codex`.
The scenario comes from FAKE_AGENT_SCENARIO: edit, idle, fail, auth, or hang.
A prompt that starts with "# Review" is answered with the next verdict in FAKE_REVIEWS: approve, changes, or garbage.
An editing or idle run also writes FAKE_OUTBOX, if set, to .hearth/outbox.jsonl as board messages.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SESSION = "00000000-0000-0000-0000-00000000000f"
CODEX = "--as" in sys.argv and sys.argv[sys.argv.index("--as") + 1] == "codex"
VERDICTS = {
    "approve": 'Looks good.\n{"verdict": "approve", "findings": []}',
    "changes": 'One problem.\n{"verdict": "changes", "findings": [{"standard": "CLI-3", "file": "hello.txt", "line": 1, "problem": "Say hello."}]}',
    "garbage": "I think it is fine.",
}


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def finish(text: str, error: bool = False) -> None:
    if CODEX:
        emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}})
        emit({"type": "turn.failed"} if error else {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}})
    else:
        emit({"type": "result", "subtype": "success", "is_error": error, "result": text, "session_id": SESSION, "usage": {"input_tokens": 3, "output_tokens": 4}})


def next_review() -> str:
    count = Path.home() / ".fake-reviews-given"
    given = int(count.read_text()) if count.exists() else 0
    count.write_text(str(given + 1))
    return os.environ.get("FAKE_REVIEWS", "approve").split(",")[given]


def main() -> int:
    prompt = sys.stdin.read()
    scenario = os.environ.get("FAKE_AGENT_SCENARIO", "edit")
    if CODEX:
        emit({"type": "thread.started", "thread_id": SESSION, "argv": sys.argv[1:]})
    else:
        emit({"type": "system", "subtype": "init", "session_id": SESSION, "model": "fake-model", "argv": sys.argv[1:], "hearth_task": os.environ.get("HEARTH_TASK")})
    if prompt.startswith("# Review"):
        Path.home().joinpath(".fake-last-review-prompt").write_text(prompt, encoding="utf-8")
        finish(VERDICTS[next_review()])
        return 0
    if os.environ.get("FAKE_OUTBOX"):
        Path(".hearth").mkdir(exist_ok=True)
        Path(".hearth/outbox.jsonl").write_text(os.environ["FAKE_OUTBOX"] + "\n", encoding="utf-8")
    if scenario == "hang":
        time.sleep(60)
    if scenario == "fail":
        finish("The provider failed.", error=True)
        return 1
    if scenario == "auth":
        finish("Failed to authenticate: OAuth session expired and could not be refreshed", error=True)
        return 1
    if scenario == "idle":
        finish("")
        return 0
    with Path("hello.txt").open("a", encoding="utf-8") as file:
        file.write(prompt.splitlines()[0] + "\n")
    Path(".hearth/artifacts").mkdir(parents=True, exist_ok=True)
    Path(".hearth/artifacts/note.txt").write_text("for the person\n", encoding="utf-8")
    finish("Added hello.txt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
