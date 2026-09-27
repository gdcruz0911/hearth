"""A stand-in provider CLI for workbench tests.

It reads the prompt on standard input and prints Claude-format stream events.
The scenario comes from FAKE_AGENT_SCENARIO: edit, idle, fail, auth, or hang.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SESSION = "00000000-0000-0000-0000-00000000000f"


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def main() -> int:
    prompt = sys.stdin.read()
    scenario = os.environ.get("FAKE_AGENT_SCENARIO", "edit")
    emit({"type": "system", "subtype": "init", "session_id": SESSION, "model": "fake-model", "argv": sys.argv[1:], "hearth_task": os.environ.get("HEARTH_TASK")})
    if scenario == "hang":
        time.sleep(60)
    if scenario == "fail":
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "The provider failed.", "session_id": SESSION})
        return 1
    if scenario == "auth":
        emit({"type": "result", "subtype": "success", "is_error": True, "result": "Failed to authenticate: OAuth session expired and could not be refreshed", "session_id": SESSION})
        return 1
    if scenario == "idle":
        emit({"type": "result", "subtype": "success", "is_error": False, "result": "", "session_id": SESSION})
        return 0
    Path("hello.txt").write_text(prompt.splitlines()[0] + "\n", encoding="utf-8")
    Path(".hearth/artifacts").mkdir(parents=True, exist_ok=True)
    Path(".hearth/artifacts/note.txt").write_text("for the person\n", encoding="utf-8")
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "Added hello.txt.", "session_id": SESSION, "usage": {"input_tokens": 3, "output_tokens": 4}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
