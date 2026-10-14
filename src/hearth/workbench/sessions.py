"""Direct agent sessions (ADR-0041, slice 1): one conversation with one official CLI, turn by turn, from the app.

A session is a task record with `kind: "session"` and its own worktree; each turn is a run in its `runs` list, launched
by the backend from the same tables as tasks (`PROVIDERS` to edit, `REVIEWERS` to read only), resumed with the CLI's own
session, and stopped with the task's process group. Turns belong to the backend that started them: quitting Hearth stops
them, and a turn found without its backend is stopped and marked interrupted, never resent.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import roles, tasks, usage

KIND = "session"
MODES = {"edit": "PROVIDERS", "read": "REVIEWERS"}  # Edit in the session's own worktree, or read it only.
PROVIDERS = ("claude", "codex")  # Resume, stop, and restrictions checked in real runs on 2026-10-13; agy is not.
EFFORTS = ("low", "medium", "high")
MESSAGE_LIMIT = 20_000
SEND_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TIMEOUT_SECONDS = 1800
WARNINGS = re.compile(r"\[claude-code:[a-z_]+\][^\n]*|unrecognized_model[^\n]*")

_active: dict[str, subprocess.Popen] = {}  # Session ID to the turn this backend is running for it.
_active_lock = threading.Lock()


class SessionError(Exception):
    """A request Hearth refuses, with what to do; `status` is the HTTP status the dashboard gets."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def options() -> dict:
    """What the new-conversation form offers: each project's allowed session CLIs and their default models."""
    projects = tasks._projects()
    found = {}
    for name, project in projects.items():
        allowed = [provider for provider in project.get("providers", []) if provider in PROVIDERS]
        found[name] = {"providers": allowed, "models": {provider: _default_model(name, provider) for provider in allowed}}
    return {"projects": found, "efforts": list(EFFORTS), "modes": list(MODES)}


def create(project: str, provider: str, mode: str, model: str | None, effort: str | None, request: str) -> dict:
    """A new session with its own worktree and no turn yet: starting a conversation runs no model (ADR-0041)."""
    projects = tasks._projects()
    if project not in projects:
        raise SessionError(f"No project {project}. Next: add it to ~/.hearth/projects.json")
    if provider not in PROVIDERS or provider not in projects[project].get("providers", []):
        raise SessionError(f"{project} does not offer {provider} for sessions. Next: choose one of: {', '.join(options()['projects'][project]['providers']) or 'none'}")
    if mode not in MODES or (effort is not None and effort not in EFFORTS) or not SEND_ID.match(request or ""):
        raise SessionError("Choose a mode, an effort, and send a request ID.")
    model = (model or "").strip() or _default_model(project, provider)
    if model and not re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", model):
        raise SessionError("A model name is letters, digits, and . _ : / - only.")
    existing = next((summary(task) for task in _sessions() if task.get("request") == request), None)
    if existing:  # The same click arriving twice makes one conversation.
        return existing
    repo = Path(projects[project]["path"]).expanduser()
    try:
        base = tasks._git(repo, "rev-parse", "HEAD").strip()
    except (OSError, subprocess.CalledProcessError):
        raise SessionError(f"{project}'s folder {repo} is not a Git repository with a commit. Next: fix its path in ~/.hearth/projects.json") from None
    session_id = time.strftime("%Y%m%d-%H%M%S")
    for suffix in range(2, 100):
        if not (tasks._home() / "tasks" / session_id).exists():
            break
        session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{suffix}"
    task_dir = tasks._home() / "tasks" / session_id
    worktree = tasks._home() / "worktrees" / project / session_id
    tasks._git(repo, "worktree", "add", "-q", "-b", f"hearth/{session_id}", str(worktree), base)
    task_dir.mkdir(parents=True)
    copied = tasks._copy_local(repo, worktree)
    task = {"id": session_id, "kind": KIND, "project": project, "goal": "New conversation", "status": "done", "stop_reason": None,
            "runs": [], "created": tasks._now(), "finished": None, "branch": f"hearth/{session_id}", "base": base,
            "worktree": str(worktree), "copied": copied, "request": request,
            "session": {"provider": provider, "mode": mode, "model": model or None, "effort": effort or "medium", "cli_session": None}}
    tasks._write(task_dir, task)
    return summary(task)


def send(session_id: str, message: str, send_id: str) -> dict:
    """Start a turn with the keeper's message; the same send ID, retried, returns that turn instead of sending again."""
    if not SEND_ID.match(send_id or ""):
        raise SessionError("A send needs a request ID.")
    message = (message or "").strip()
    if not message or len(message) > MESSAGE_LIMIT:
        raise SessionError(f"Write a message of up to {MESSAGE_LIMIT} characters.")
    task_dir = _dir(session_id)
    with tasks._task_lock(task_dir):
        task = tasks._read(task_dir)
        repeat = next((run for run in task["runs"] if run.get("send_id") == send_id), None)
        if repeat is not None:
            return summary(task)  # Recorded already, in whatever state it is in, including interrupted: never relaunched.
        if task["runs"] and not task["runs"][-1].get("finished"):
            raise SessionError("A turn is still running in this conversation. Next: wait for it, or stop it.", 409)
        settings = task["session"]
        tampered = tasks._tampered(task, task_dir)  # The same refusal as a task's: agent-written settings, or an unlinked sign-in.
        if tampered:
            raise SessionError(f"Hearth will not continue this conversation: {tampered[1]} Next: start a new conversation.", 409)
        slot = "implement" if settings["mode"] == "edit" else "support"
        if tasks._busy(slot) >= tasks.SLOTS[slot]:
            raise SessionError(f"All {tasks.SLOTS[slot]} {slot} slots are in use. Next: wait for a run to finish, or stop one.", 409)
        spent = [row for row in usage.report(usage.codex_limits(*usage.codex_sessions()), usage.claude_limits(tasks._home() / "usage/claude-limits.jsonl"))
                 if row["provider"] == settings["provider"] and max(row["five_hour"] or 0, row["week"] or 0) >= tasks.HEADROOM_LIMIT]
        if spent:
            raise SessionError(f"{settings['provider']} is at {tasks.HEADROOM_LIMIT}% or more of a plan limit. Next: hearth usage, or wait for it to reset.", 409)
        run_dir = f"{len(task['runs']) + 1:02d}-session-{settings['provider']}"
        (task_dir / "runs" / run_dir).mkdir(parents=True)
        (task_dir / "runs" / run_dir / "prompt.md").write_text(message, encoding="utf-8")
        record = {"dir": run_dir, "role": KIND, "provider": settings["provider"], "model": settings["model"], "effort": settings["effort"],
                  "started": tasks._now(), "finished": None, "pid": None, "send_id": send_id, "message": message, "resumed": settings["cli_session"]}
        task["runs"].append(record)  # Recorded before launch, so a retry after a crash finds it and never sends twice.
        if task["goal"] == "New conversation":
            words = " ".join(message.split())
            task["goal"] = words if len(words) <= 80 else words[:79].rsplit(" ", 1)[0] + "…"
        task["status"] = "running"
        tasks._write(task_dir, task)
        try:
            process = _launch(task, task_dir, record, message)
        except OSError as exc:
            record.update(finished=tasks._now(), outcome="failed", error=f"The {settings['provider']} command could not start: {exc}")
            task["status"] = "done"
            tasks._write(task_dir, task)
            return summary(task)
        # The backend that owns the turn: another backend on the same home must not take it for a crash's leftover.
        record.update(pid=process.pid, pid_identity=tasks._identity(process.pid), owner=os.getpid(), owner_identity=tasks._identity(os.getpid()))
        tasks._write(task_dir, task)
    with _active_lock:
        _active[session_id] = process
    threading.Thread(target=_wait, args=(session_id, process), daemon=True).start()
    return summary(task)


def stop(session_id: str, run: str) -> dict:
    """Stop the session's running turn, keeping what it wrote; stopping a finished turn returns its state."""
    task_dir = _dir(session_id)
    with tasks._task_lock(task_dir):
        task = tasks._read(task_dir)
        record = next((item for item in task["runs"] if item.get("dir") == run), None)
        if record is None:
            raise SessionError("That turn is not in this conversation.", 404)
        if record.get("finished"):
            return summary(task)
        record["stopping"] = "keeper"
        tasks._write(task_dir, task)
    with _active_lock:
        process = _active.get(session_id)
    if process is not None:
        tasks._stop(process)  # SIGTERM to the turn's process group, then SIGKILL; _wait records the outcome.
        _wait(session_id, process)
    else:  # A turn this backend did not start, such as one left by a crash.
        _settle(session_id)
    return summary(tasks._read(task_dir))


def stop_all() -> None:
    """When the backend stops, stop every turn it started (ADR-0041), keeping their output; nothing is resent later."""
    with _active_lock:
        running = list(_active.items())
    for session_id, process in running:
        try:
            with tasks._task_lock(_dir(session_id)):
                task = tasks._read(_dir(session_id))
                task["runs"][-1]["stopping"] = "quit"
                tasks._write(_dir(session_id), task)
        except (OSError, ValueError, KeyError, IndexError, SessionError):
            pass  # Its record is unreadable or gone; the process is still stopped.
        tasks._stop(process)
        _wait(session_id, process)


def recover() -> None:
    """At backend start: a turn left running by a crashed backend is stopped by its recorded identity and marked interrupted."""
    for task in _sessions():
        last = task["runs"][-1] if task["runs"] else {}
        if last and not last.get("finished") and not _owned_elsewhere(last):
            _settle(task["id"])


def _owned_elsewhere(record: dict) -> bool:
    """Whether the backend that started the turn still runs, so the turn is its to finish, not a crash's leftover."""
    owner = record.get("owner")
    return bool(owner and owner != os.getpid() and record.get("owner_identity") and tasks._identity(owner) == record["owner_identity"])


def listing() -> dict:
    return {"sessions": [summary(task) for task in _sessions()], "options": options()}


def detail(session_id: str) -> dict:
    return summary(tasks._read(_dir(session_id)), turns=True)


def summary(task: dict, turns: bool = False) -> dict:
    last = task["runs"][-1] if task["runs"] else None
    running = bool(last and not last.get("finished"))
    found = {"id": task["id"], "project": task["project"], "title": task["goal"], "created": task["created"],
             # CLI-5's words: running during a turn, otherwise how the last turn ended; None before the first turn.
             "status": ("running" if task["status"] == "running" else "interrupted") if running else (last or {}).get("outcome"),
             "provider": task["session"]["provider"], "mode": task["session"]["mode"], "model": task["session"]["model"],
             "effort": task["session"]["effort"], "resumable": bool(task["session"]["cli_session"]), "turn_count": len(task["runs"]),
             "updated": (last or {}).get("finished") or (last or {}).get("started") or task["created"], "worktree": task["worktree"]}
    if turns:
        found["turns"] = [{key: run.get(key) for key in ("dir", "message", "model", "effort", "started", "finished", "outcome", "exit_code",
                                                          "model_used", "sandbox_used", "warnings", "error", "session_id", "resumed", "stopped_by")}
                          for run in task["runs"]]
    return found


def _launch(task: dict, task_dir: Path, record: dict, message: str) -> subprocess.Popen:
    settings, project = task["session"], tasks._projects()[task["project"]]
    template = getattr(tasks, MODES[settings["mode"]])[settings["provider"]]
    argv = tasks._argv(template, settings["provider"], message, project.get("check") or "true", settings["model"], settings["effort"],
                       settings=tasks._claude_settings(Path(task["worktree"])))
    argv = _resumable(argv, settings["provider"], settings["cli_session"])
    record["argv"] = argv  # It holds no message text, which goes on stdin.
    run_dir = task_dir / "runs" / record["dir"]
    events, errors = (run_dir / "events.jsonl").open("w", encoding="utf-8"), (run_dir / "stderr.txt").open("w", encoding="utf-8")
    env = {**os.environ, **tasks.NO_BYTECODE, "HEARTH_TASK": task["id"], "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    if settings["provider"] == "codex":
        env["CODEX_HOME"] = str(tasks._codex_home(task_dir))
    try:
        process = subprocess.Popen(argv, cwd=task["worktree"], stdin=subprocess.PIPE, stdout=events, stderr=errors, text=True,
                                   start_new_session=True, env=env)
    finally:
        events.close()
        errors.close()
    process.stdin.write(message)
    process.stdin.close()
    return process


def _resumable(argv: list[str], provider: str, cli_session: str | None) -> list[str]:
    """The table's invocation, made to stream partial text and to continue the CLI's own session after the first turn."""
    argv = list(argv)
    if provider == "claude":
        argv.insert(argv.index("--verbose") + 1, "--include-partial-messages")  # Text as it arrives, kept if the turn is stopped.
        if cli_session:
            argv[1:1] = ["--resume", cli_session]
    elif provider == "codex" and cli_session:
        # `codex exec resume` has no --sandbox flag; the same mode goes through -c, which held on 2026-10-13's real runs.
        index = argv.index("--sandbox")
        argv[index:index + 2] = ["-c", f'sandbox_mode="{argv[index + 1]}"']
        argv[1:2] = ["exec", "resume", cli_session]
    return argv


def _wait(session_id: str, process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        tasks._stop(process)
        process.wait()
    with _active_lock:
        if _active.get(session_id) is process:
            del _active[session_id]
    try:
        _settle(session_id, process.returncode)
    except SessionError:
        pass  # The conversation's record is gone, so there is nothing left to record the end in.


def _settle(session_id: str, exit_code: int | None = None) -> None:
    """Record how the last turn ended, once: from its own exit, or, for a turn no longer owned, by stopping what is left."""
    task_dir = _dir(session_id)
    with tasks._task_lock(task_dir):
        task = tasks._read(task_dir)
        if not task["runs"] or task["runs"][-1].get("finished"):
            return
        record = task["runs"][-1]
        owned = exit_code is not None
        if not owned and record.get("pid"):
            tasks._stop_pid(record["pid"], record.get("pid_identity"))  # Only while the process still is that turn.
        run_dir = task_dir / "runs" / record["dir"]
        text = (run_dir / "events.jsonl").read_text(encoding="utf-8", errors="replace") if (run_dir / "events.jsonl").exists() else ""
        parsed = tasks.parse_events(record["provider"], text)
        errors = (run_dir / "stderr.txt").read_text(encoding="utf-8", errors="replace") if (run_dir / "stderr.txt").exists() else ""
        if record.get("stopping"):
            outcome = "cancelled"  # CLI-5's word for work the keeper stopped, or quitting did.
        elif not owned:
            outcome = "interrupted"
        elif exit_code != 0 or parsed["error"]:
            outcome = "failed"
        else:
            outcome = "done"
        record.update(finished=tasks._now(), exit_code=exit_code, outcome=outcome, session_id=parsed["session_id"],
                      warnings=sorted(set(WARNINGS.findall(errors))) or None, stopped_by=record.pop("stopping", None))
        if outcome == "failed":
            record["error"] = parsed["error"] or (errors.strip().splitlines() or [f"Exit code {exit_code}"])[-1][:500]
        if record["provider"] == "codex":
            record["model_used"], record["sandbox_used"] = _codex_context(task_dir, parsed["session_id"])
        else:
            record["model_used"] = parsed["model"]
        if parsed["session_id"] and not task["session"]["cli_session"]:
            task["session"]["cli_session"] = parsed["session_id"]
        task["status"] = "done"  # Between turns a session is ready, never waiting or queued, so an update is not refused.
        tasks._write(task_dir, task)


def _codex_context(task_dir: Path, thread: str | None) -> tuple[str | None, str | None]:
    """The model and sandbox Codex recorded for its latest turn, from this session's own CODEX_HOME; None when not recorded."""
    if not thread:
        return None, None
    model = sandbox = None
    for path in sorted((task_dir / "codex-home" / "sessions").glob(f"**/rollout-*{thread}.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "turn_context" and isinstance(event.get("payload"), dict):
                model = event["payload"].get("model") or model
                sandbox = (event["payload"].get("sandbox_policy") or {}).get("type") or sandbox
    return model, sandbox


def _default_model(project: str, provider: str) -> str | None:
    try:
        implement = (roles.load(tasks._home(), project).get("implement") or [{}])[0]
    except roles.RolesError:
        implement = {}
    return (implement.get("model") if implement.get("provider") == provider else None) or tasks._model(provider, "implement")


def _dir(session_id: str) -> Path:
    task_dir = tasks._home() / "tasks" / session_id
    if not tasks.TASK_ID.match(session_id or "") or not (task_dir / "task.json").is_file():
        raise SessionError("No such conversation.", 404)
    if json.loads((task_dir / "task.json").read_text(encoding="utf-8")).get("kind") != KIND:
        raise SessionError("That is a task, not a conversation.", 404)
    return task_dir


def _sessions() -> list[dict]:
    found = []
    for path in sorted(tasks._home().glob("tasks/*/task.json"), reverse=True):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("kind") == KIND:
                found.append(tasks._read(path.parent))
        except (OSError, ValueError, tasks.NewerTaskError):
            continue
    return found
