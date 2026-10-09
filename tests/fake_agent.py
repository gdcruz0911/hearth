"""A stand-in provider CLI for workbench tests.

It reads the prompt on standard input and prints Claude-format stream events, or another CLI's after `--as codex` or `--as antigravity`.
The scenario comes from FAKE_AGENT_SCENARIO: edit, rewrite, idle, fail, auth, or hang.
A prompt that starts with "# Review" is answered with the next verdict in FAKE_REVIEWS: approve, changes, elsewhere, second-line, or garbage.
A prompt that starts with "# Verify" follows the next scenario in FAKE_VERIFY: verified, failed, missing, edit, cache, or garbage.
A test-writing prompt writes tests/test_hello.txt, plus notes.txt when FAKE_TESTS is "source".
A prompt that starts with "# Ask" is answered with FAKE_ASK, by default a reply citing excerpt [1].
A prompt that starts with "# Retro" is answered with the next reply in FAKE_RETRO: proposal, guard, or garbage.
An editing or idle run also writes FAKE_OUTBOX, if set, to .hearth/outbox.jsonl as board messages,
and an editing run appends FAKE_EXTRA, if set, to hello.txt, and ends with a PR title line unless FAKE_TITLE is none.
An editing run first runs FAKE_RUN, if set, as a shell command in the worktree.
Every run first waits FAKE_DELAY seconds, if set.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SESSION = "00000000-0000-0000-0000-00000000000f"
FORMAT = sys.argv[sys.argv.index("--as") + 1] if "--as" in sys.argv else "claude"
VERDICTS = {
    "approve": 'Looks good.\n{"verdict": "approve", "findings": [], "risk": "low: only adds hello.txt"}',
    "changes": 'One problem.\n{"verdict": "changes", "findings": [{"standard": "CLI-3", "file": "hello.txt", "line": 1, "problem": "Say hello."}]}',
    "elsewhere": 'One problem.\n{"verdict": "changes", "findings": [{"standard": "CLI-3", "file": "notes.txt", "line": 1, "problem": "Add notes."}]}',
    "second-line": 'One problem.\n{"verdict": "changes", "findings": [{"standard": "CLI-3", "file": "hello.txt", "line": 2, "problem": "Line two is wrong."}]}',
    "garbage": "I think it is fine.",
}


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def finish(text: str, error: bool = False) -> None:
    if FORMAT == "antigravity":
        emit({"event": "result", "result": {"conversation_id": SESSION, "status": "ERROR" if error else "SUCCESS", "response": text}})
    elif FORMAT == "codex":
        emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}})
        emit({"type": "turn.failed"} if error else {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}})
    else:
        emit({"type": "result", "subtype": "success", "is_error": error, "result": text, "session_id": SESSION, "usage": {"input_tokens": 3, "output_tokens": 4}})


def next_answer(name: str, default: str) -> str:
    count = Path.home() / f".fake-{name}-given"
    given = int(count.read_text()) if count.exists() else 0
    count.write_text(str(given + 1))
    return os.environ.get(f"FAKE_{name.upper()}", default).split(",")[given]


def verify() -> None:
    scenario = next_answer("verify", "verified")
    if scenario == "garbage":
        finish("It works, I think.")
        return
    if scenario == "edit":
        Path("hello.txt").write_text("changed by the verifier\n", encoding="utf-8")
    if scenario == "cache":
        Path("__pycache__").mkdir(exist_ok=True)
        Path("__pycache__/hello.cpython-314.pyc").write_bytes(b"written by running the program")
    if scenario != "missing":
        Path(".hearth/evidence").mkdir(parents=True, exist_ok=True)
        observed = Path("hello.txt").read_text(encoding="utf-8") if Path("hello.txt").exists() else "observed output\n"
        Path(".hearth/evidence/hello.txt").write_text(observed, encoding="utf-8")
    result = "fail" if scenario == "failed" else "pass"
    claims = [{"claim": "hello.txt greets the person", "evidence": "evidence/hello.txt", "result": result}]
    verdict = "failed" if scenario == "failed" else "verified"
    finish("Checked.\n" + json.dumps({"verdict": verdict, "claims": claims, "not_checked": ["the web interface: no loopback port here"]}))


def main() -> int:
    prompt = sys.stdin.read()
    scenario = os.environ.get("FAKE_AGENT_SCENARIO", "edit")
    time.sleep(float(os.environ.get("FAKE_DELAY", "0")))  # Seconds every run takes, for tests that act while work is still going.
    if FORMAT == "antigravity":
        emit({"event": "init", "conversation_id": SESSION, "init": {"argv": sys.argv[1:]}})
    elif FORMAT == "codex":
        emit({"type": "thread.started", "thread_id": SESSION, "argv": sys.argv[1:]})
    else:
        emit({"type": "system", "subtype": "init", "session_id": SESSION, "model": "fake-model", "argv": sys.argv[1:], "hearth_task": os.environ.get("HEARTH_TASK")})
    if prompt.startswith("# Review"):
        Path.home().joinpath(".fake-last-review-prompt").write_text(prompt, encoding="utf-8")
        finish(VERDICTS[next_answer("reviews", "approve")])
        return 0
    if prompt.startswith("# Ask"):
        Path.home().joinpath(".fake-last-ask-prompt").write_text(prompt, encoding="utf-8")
        finish(os.environ.get("FAKE_ASK", "Greetings are warm [1]."))
        return 0
    if prompt.startswith("# Retro"):
        Path.home().joinpath(".fake-last-retro-prompt").write_text(prompt, encoding="utf-8")
        Path.home().joinpath(".fake-last-retro-cwd").write_text(str(Path.cwd()), encoding="utf-8")
        reply = next_answer("retro", "proposal")
        kind = "guard" if reply == "guard" else "test"
        proposal = {"proposals": [{"escape": 1, "kind": kind, "where": "tests/test_hello.py",
                                   "change": "Add a regression test that hello.txt greets by name."}]}
        finish("Retro.\n" + json.dumps(proposal) if reply in ("proposal", "guard") else "Nothing to add.")
        return 0
    if prompt.startswith("# Verify"):
        Path.home().joinpath(".fake-last-verify-prompt").write_text(prompt, encoding="utf-8")
        verify()
        return 0
    if "write the tests first" in prompt.splitlines()[0]:
        Path("tests").mkdir(exist_ok=True)
        Path("tests/test_hello.txt").write_text("hello.txt must exist\n", encoding="utf-8")
        if os.environ.get("FAKE_TESTS") == "source":
            Path("notes.txt").write_text("not a test\n", encoding="utf-8")
        finish("Wrote tests/test_hello.txt; it fails because hello.txt does not exist yet.")
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
    if os.environ.get("FAKE_RUN"):
        subprocess.run(os.environ["FAKE_RUN"], shell=True, check=True)  # The worker runs the project's code, as real agents do.
    if scenario == "rewrite":
        Path("hello.txt").write_text("rewritten\n", encoding="utf-8")
    with Path("hello.txt").open("a", encoding="utf-8") as file:
        file.write(prompt.splitlines()[0] + "\n" + os.environ.get("FAKE_EXTRA", ""))
    Path(".hearth/artifacts").mkdir(parents=True, exist_ok=True)
    Path(".hearth/artifacts/note.txt").write_text("for the person\n", encoding="utf-8")
    title = "" if os.environ.get("FAKE_TITLE") == "none" else "\nPR title: feat: add hello.txt\nPR summary: hello.txt now greets the person."
    finish("Added hello.txt." + title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
