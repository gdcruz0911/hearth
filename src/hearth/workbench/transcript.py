"""A run's session as display items, read incrementally from its `events.jsonl` while the agent works.

Transcripts can hold recalled excerpts and file contents, so they are for the person's dashboard only, never the
supervisor's status summary (ADR-0033).
"""

from __future__ import annotations

import json
import re

from . import tasks

LIMIT = 256_000  # Bytes read per call; the page asks again from the returned offset.
TEXT = 20_000  # Characters kept per item.
DIFF_LIMIT = 400_000  # Bytes of a task's diff the dashboard shows; a larger diff is cut at a line and says so.
TASK_ID = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")


def read(task_id: str, run: str, offset: int) -> dict | None:
    """Complete events of one run after `offset`, translated, with the next offset; None for an unknown task."""
    if not TASK_ID.match(task_id) or offset < 0:
        raise ValueError("Unknown task or offset.")
    task_dir = tasks._home() / "tasks" / task_id
    if not (task_dir / "task.json").is_file():
        return None
    task = tasks._read(task_dir)
    record = next((item for item in task["runs"] if item.get("dir") == run), None)
    if record is None:
        raise ValueError("That run is not one of this task's runs.")  # Only recorded run folders are ever opened.
    path = task_dir / "runs" / run / "events.jsonl"
    chunk = b""
    if path.exists():
        with path.open("rb") as events:
            events.seek(offset)
            chunk = events.read(LIMIT)
    complete = chunk[: chunk.rfind(b"\n") + 1]  # A line still being written waits for the next call.
    items = [item for line in complete.decode("utf-8", errors="replace").splitlines() for item in _items(record["provider"], line)]
    return {"task": task_id, "run": run, "role": record["role"], "provider": record["provider"], "items": items,
            "offset": offset + len(complete), "finished": bool(record.get("finished"))}


def diff(task_id: str) -> dict | None:
    """The task's saved diff for the person's dashboard, with its line counts; None for an unknown task."""
    if not TASK_ID.match(task_id):
        raise ValueError("Unknown task.")
    task_dir = tasks._home() / "tasks" / task_id
    if not (task_dir / "task.json").is_file():
        return None
    path = task_dir / "diff.patch"
    if not path.exists():
        return {"task": task_id, "diff": None, "added": 0, "removed": 0, "truncated": False}
    raw = path.read_bytes()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    shown = raw[:DIFF_LIMIT]
    shown = shown if len(raw) <= DIFF_LIMIT else shown[: shown.rfind(b"\n") + 1]
    return {"task": task_id, "diff": shown.decode("utf-8", errors="replace"),
            "added": sum(line.startswith("+") and not line.startswith("+++") for line in lines),
            "removed": sum(line.startswith("-") and not line.startswith("---") for line in lines),
            "truncated": len(raw) > DIFF_LIMIT}


def _items(provider: str, line: str) -> list[dict]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, dict):
        return []
    found = {"claude": _claude, "codex": _codex, "antigravity": _antigravity}.get(provider, lambda event: [])(event)
    return [{**item, "text": _clip(item.get("text"))} for item in found]


def _claude(event: dict) -> list[dict]:
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return [{"kind": "start", "text": f"Session started on {event.get('model') or 'an unreported model'}."}]
    if kind == "result":
        return [{"kind": "result", "text": event.get("result"), "error": bool(event.get("is_error"))}]
    items = []
    for part in ((event.get("message") or {}).get("content") or []) if kind in ("assistant", "user") else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            items.append({"kind": "message", "text": part.get("text")})
        elif part.get("type") == "tool_use":
            arguments = part.get("input") or {}
            items.append({"kind": "tool", "tool": part.get("name"),
                          "text": arguments.get("command") or arguments.get("file_path") or json.dumps(arguments)})
        elif part.get("type") == "tool_result":
            content = part.get("content")
            if isinstance(content, list):
                content = "\n".join(piece.get("text", "") for piece in content if isinstance(piece, dict))
            items.append({"kind": "output", "text": content, "error": bool(part.get("is_error"))})
    return items


def _codex(event: dict) -> list[dict]:
    kind, item = event.get("type"), event.get("item") or {}
    if kind == "thread.started":
        return [{"kind": "start", "text": "Session started."}]
    if kind in ("turn.failed", "error"):
        error = event.get("error") or {}
        return [{"kind": "error", "text": error.get("message") if isinstance(error, dict) else event.get("message")}]
    if item.get("type") == "command_execution":
        if kind == "item.started":
            return [{"kind": "tool", "tool": "shell", "text": item.get("command")}]
        if kind == "item.completed":
            return [{"kind": "output", "text": item.get("aggregated_output"), "exit_code": item.get("exit_code"),
                     "error": item.get("exit_code") not in (0, None)}]
    if kind == "item.completed" and item.get("type") == "agent_message":
        return [{"kind": "message", "text": item.get("text")}]
    if kind == "item.completed" and item.get("type") == "file_change":
        changes = item.get("changes") or []
        return [{"kind": "tool", "tool": "edit", "text": ", ".join(str(change.get("path")) for change in changes if isinstance(change, dict))}]
    return []


def _antigravity(event: dict) -> list[dict]:
    kind = event.get("event")
    if kind == "init":
        return [{"kind": "start", "text": "Session started."}]
    if kind == "result":
        outcome = event.get("result") or {}
        return [{"kind": "result", "text": outcome.get("response"), "error": outcome.get("status") != "SUCCESS"}]
    step = event.get("step_update") or {}
    if kind != "step_update":
        return []
    if step.get("step_type") == "agent_response" and step.get("text_delta"):
        return [{"kind": "message", "text": step["text_delta"]}]
    if step.get("step_type") == "tool":
        info = step.get("tool_info") or {}
        if step.get("state") == "ACTIVE":
            return [{"kind": "tool", "tool": step.get("tool_name"), "text": json.dumps(info.get("parameters") or {})}]
        if step.get("state") == "DONE":
            return [{"kind": "output", "text": info.get("output")}]
    return []


def _clip(text: object) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= TEXT else text[:TEXT] + f"\n[{len(text) - TEXT} more characters in the run's events.jsonl]"
