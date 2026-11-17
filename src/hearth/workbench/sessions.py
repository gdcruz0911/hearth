"""Direct agent sessions (ADR-0041, slice 1): one conversation with one official CLI, turn by turn, from the app.

A session is a task record with `kind: "session"` and its own worktree; each turn is a run in its `runs` list, launched
by the backend from the same tables as tasks (`PROVIDERS` to edit, `REVIEWERS` to read only), except that Claude edits with
the keeper's own setup (`CONVERSATION`, ADR-0049) and full access runs any command unasked and unsandboxed (`FULL_ACCESS`),
resumed with the CLI's own session, and stopped with the task's process group. Turns belong to the backend that started them: quitting Hearth stops them, and a turn found without its backend is
stopped and marked interrupted, never resent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import roles, tasks, usage

KIND = "session"
# Any command without asking, edit in the session's own worktree, or read it only; each names its table, in tasks except FULL_ACCESS.
MODES = {"full": "FULL_ACCESS", "edit": "PROVIDERS", "read": "REVIEWERS"}
PROVIDERS = ("claude", "codex")  # Resume, stop, and restrictions checked in real runs on 2026-10-13; agy is not.
# Hearth checks effort itself: on 2026-11-06 Claude Code 2.1.283 dropped an unknown --effort with only a warning, and Codex 0.163.0
# ran a level its catalog does not list for that model. Codex's list for a model it names comes from that catalog; this is its fallback.
EFFORTS = {"claude": ("low", "medium", "high", "xhigh", "max"), "codex": ("low", "medium", "high", "xhigh", "max", "ultra")}
CLAUDE_MODELS = ("fable", "opus", "sonnet", "haiku")  # The documented aliases: Claude Code has no command that lists models.
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,79}")  # It follows a flag in the argv, so it never starts with a dash.
CATALOG_SECONDS = 600  # The page polls the listing every few seconds; `codex debug models` takes about half a second.
CATALOG_TIMEOUT = 5
MESSAGE_LIMIT = 20_000
SEND_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TIMEOUT_SECONDS = 1800
UNTRACKED_LIMIT = 200  # New files the Changes view lists; more are counted, not shown.
WARNINGS = re.compile(r"\[claude-code:[a-z_]+\][^\n]*|unrecognized_model[^\n]*")
CHECK = "check"  # ADR-0047: the role of the run in which Hearth runs the project's check after a Codex turn, outside the sandbox.
SUMMARY_LINES = 20  # Lines of that check's output that open the keeper's next message.
# ADR-0049: a Claude conversation that edits runs with the keeper's own Claude setup (settings, CLAUDE.md, skills, plugins, MCP,
# memory, subagents), confined to its worktree by Claude Code's own working-folder rules; tasks keep their stripped tables.
# Tools that act beyond this Mac on their own (publish, notify, schedule, message) are off; probed on 2026-10-31, web tools and
# MCP calls are refused without approval, and these were not probed because trying them would act.
CONVERSATION = {"claude": ["claude", "{options}", "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits",
                           "--settings", "{settings}", "--allowedTools", "Bash({check} *)", "--disallowedTools",
                           "Artifact,ArtifactComments,ArtifactData,DesignSync,PushNotification,RemoteTrigger,CronCreate,CronDelete,ScheduleWakeup,SendMessage"]}
# Full access, the keeper's choice over ADR-0045's "every command asks": the agent runs any command as the keeper, with no approval
# and no sandbox, in the conversation's own worktree. Claude keeps the keeper's own setup and the same tools off.
FULL_ACCESS = {"claude": ["claude", "{options}", "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "bypassPermissions",
                          "--disallowedTools", CONVERSATION["claude"][-1]],
               "codex": ["danger-full-access" if part == "workspace-write" else part for part in tasks.PROVIDERS["codex"]]}
AGENT_CONFIG = (".claude", ".mcp.json")  # What a full-setup turn loads from the worktree, so the agent must not change it.

# ADR-0045: an approved command runs in Claude Code's sandbox, never as the keeper, and a turn fails rather than run without it.
SANDBOX = {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False, "autoAllowBashIfSandboxed": False,
           "credentials": {"files": [{"path": path, "mode": "deny"} for path in ("~/.ssh", "~/.aws", "~/.config/gh", "~/Library/Keychains")],
                           "envVars": [{"name": name, "mode": "deny"} for name in ("GITHUB_TOKEN", "GH_TOKEN")]}}
# ADR-0046: what Git keeps at the root of a repository's shared .git, or creates there only when needed (gitrepository-layout).
GIT_ROOT_NAMES = ("index", "HEAD", "config", "config.worktree", "description", "hooks", "info", "branches", "remotes", "modules", "shallow",
                  "packed-refs", "ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "AUTO_MERGE", "CHERRY_PICK_HEAD",
                  "REVERT_HEAD", "BISECT_LOG", "BISECT_START", "BISECT_TERMS", "BISECT_EXPECTED_REV", "COMMIT_EDITMSG", "rebase-merge",
                  "rebase-apply", "sequencer", "gc.pid", "gc.log")
GIT_REF_AREAS = ("refs/tags", "refs/remotes", "refs/replace", "refs/notes", "refs/stash")
EXPIRY_SECONDS = 600  # ADR-0045: a request nobody answers is refused after this long, and the turn goes on.
INITIALIZE_SECONDS = 30
WRITE_SECONDS = 5  # A reply Claude Code does not take within this long is recorded as not delivered, so Stop never waits on it.
REFUSALS = {"keeper": "The keeper declined this command.", "expiry": "Nobody answered in time.", "policy": "Hearth does not allow this."}
STOPPED = {"keeper": "stop", "quit": "quit", "timeout": "timeout"}  # A turn's `stopping` mark to who cancelled its pending requests.
GUARD_LIMIT = 20_000  # Paths in the list; the hearth repository had about 600 on 2026-11-15. Not yet measured against a real turn.

_active: dict[str, subprocess.Popen] = {}  # Session ID to the turn, or the check after it, this backend is running for it.
_active_lock = threading.Lock()
_quitting = False  # Set under _active_lock when the backend stops, so no check starts after stop_all has looked.
_streams: dict[str, dict] = {}  # Session ID to its stream-json turn: the process, and each pending request's input and timer.
_stdin_lock = threading.Lock()  # One writer to an agent's stdin at a time, and never a write after its close.
_create_lock = threading.Lock()  # A retry arriving while the first request still runs must find the conversation it made.
_catalog: dict[str, tuple[float, list[dict]]] = {}  # Codex's program path to when it was read and the models it listed.
_catalog_lock = threading.Lock()


class LaunchRefused(Exception):
    """A turn Hearth will not start, by its named error."""


class SessionError(Exception):
    """A request Hearth refuses, with what to do; `status` is the HTTP status the dashboard gets."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def options() -> dict:
    """What a new chat offers: each project's allowed session CLIs and default models, and each CLI's models and effort levels."""
    # Before any project is added the form says how to add one, rather than the whole view failing.
    projects = tasks._projects() if (tasks._home() / "projects.json").exists() else {}
    found = {}
    for name, project in projects.items():
        allowed = [provider for provider in project.get("providers", []) if provider in PROVIDERS]
        found[name] = {"providers": allowed, "models": {provider: _default_model(name, provider) for provider in allowed}}
    return {"projects": found, "modes": list(MODES),
            "agents": {"claude": {"models": [{"id": name} for name in CLAUDE_MODELS], "efforts": list(EFFORTS["claude"])},
                       "codex": {"models": _codex_models(), "efforts": list(EFFORTS["codex"])}}}


def _codex_models() -> list[dict]:
    """The models `codex debug models` offers this account, with each one's effort levels; none when Codex cannot say, so the
    picker falls back to the project's default, recently used models, and typing a name. Checked on Codex 0.163.0, 2026-11-06."""
    program = tasks.PROVIDERS["codex"][0]
    with _catalog_lock:
        read_at, models = _catalog.get(program, (None, []))
        if read_at is not None and time.monotonic() - read_at < CATALOG_SECONDS:
            return models
        try:
            listed = subprocess.run([program, "debug", "models"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=CATALOG_TIMEOUT)
            models = _parse_catalog(listed.stdout) if listed.returncode == 0 else []
        except (OSError, subprocess.SubprocessError, ValueError):
            models = []
        _catalog[program] = (time.monotonic(), models)
        return models


def _parse_catalog(text: str) -> list[dict]:
    rows = json.loads(text)
    rows = rows.get("models") if isinstance(rows, dict) else None
    models = []
    for row in rows if isinstance(rows, list) else []:  # CODE-3: this debug command's shape may change.
        if not isinstance(row, dict) or row.get("visibility") != "list" or not MODEL_NAME.fullmatch(str(row.get("slug"))):
            continue
        levels = row.get("supported_reasoning_levels")
        efforts = [level.get("effort") for level in levels if isinstance(level, dict)] if isinstance(levels, list) else []
        models.append({"id": row["slug"], "efforts": [effort for effort in efforts if isinstance(effort, str) and re.fullmatch("[a-z]+", effort)]})
    return models


def _efforts(provider: str, model: str | None) -> list[str]:
    listed = next((row["efforts"] for row in _codex_models() if row["id"] == model), None) if provider == "codex" else None
    return listed or list(EFFORTS[provider])


def _choice(project: str, provider: str, model: str | None, effort: str) -> tuple[str | None, str]:
    """A model and effort Hearth will pass to the CLI, or the refusal; an empty model means the project's default."""
    model = (model or "").strip() or _default_model(project, provider)
    if model and not MODEL_NAME.fullmatch(model):
        raise SessionError("A model name starts with a letter or digit, and has only letters, digits, and . _ : / [ ] -")
    allowed = _efforts(provider, model)
    if effort not in allowed:
        raise SessionError(f"{model or 'This model'} does not offer {effort} effort. Choose one of: {', '.join(allowed)}.")
    return model, effort


def create(project: str, provider: str, mode: str, model: str | None, effort: str | None, request: str) -> dict:
    """A new session with its own worktree and no turn yet: starting a conversation runs no model (ADR-0041)."""
    projects = tasks._projects()
    if project not in projects:
        raise SessionError(f"No project {project}. Next: add it to ~/.hearth/projects.json")
    if provider not in PROVIDERS or provider not in projects[project].get("providers", []):
        raise SessionError(f"{project} does not offer {provider} for sessions. Next: choose one of: {', '.join(options()['projects'][project]['providers']) or 'none'}")
    if mode not in MODES or not SEND_ID.match(request or ""):
        raise SessionError("Choose what it can do, then try again. If this keeps happening, reload the window.")
    model, effort = _choice(project, provider, model, "medium" if effort is None else effort)
    with _create_lock:
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
                "session": {"provider": provider, "mode": mode, "model": model or None, "effort": effort, "cli_session": None}}
        tasks._write(task_dir, task)
        return summary(task)


def choose(session_id: str, model: str | None, effort: str | None) -> dict:
    """Save the model and effort for this conversation's next message; a turn already running keeps what it started with (ADR-0041)."""
    task_dir = _dir(session_id)
    with tasks._task_lock(task_dir):
        task = tasks._read(task_dir)
        _apply_choice(task, model, effort)
        tasks._write(task_dir, task)
        return summary(task)


def _apply_choice(task: dict, model: str | None, effort: str | None) -> None:
    settings = task["session"]
    settings["model"], settings["effort"] = _choice(task["project"], settings["provider"], settings["model"] if model is None else model,
                                                    settings["effort"] if effort is None else effort)


def send(session_id: str, message: str, send_id: str, draft_at: int | None = None, model: str | None = None, effort: str | None = None) -> dict:
    """Start a turn with the keeper's message; the same send ID, retried, returns that turn instead of sending again.

    `draft_at` is the draft version the message came from: a started turn clears that draft, and no older save brings it back.
    `model` and `effort` are what the message box showed: they start with this message and stay for the next ones.
    """
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
            if task["runs"][-1]["role"] == CHECK:
                raise SessionError("Hearth is still running the project's check after the last turn. Next: wait for it, or stop it.", 409)
            raise SessionError("A turn is still running in this conversation. Next: wait for it, or stop it.", 409)
        _apply_choice(task, model, effort)  # In this lock, after the repeat check: a refusal or a retry changes nothing.
        settings = task["session"]
        tampered = tasks._tampered(task, task_dir)  # The same refusal as a task's: agent-written settings, or an unlinked sign-in.
        if tampered:
            raise SessionError(f"Hearth will not continue this conversation: {tampered[1]} Next: start a new conversation.", 409)
        if _full_setup(settings) and (changed := _config_changed(task)):
            raise SessionError(f"Hearth will not continue this conversation: {changed} in its worktree differs from the base commit, and "
                               "Claude would load it as its own configuration. Next: start a new conversation.", 409)
        slot = "implement" if settings["mode"] in ("edit", "full") else "support"
        if tasks._busy(slot) >= tasks.SLOTS[slot]:
            raise SessionError(f"All {tasks.SLOTS[slot]} {slot} slots are in use. Next: wait for a run to finish, or stop one.", 409)
        run_dir = f"{len(task['runs']) + 1:02d}-session-{settings['provider']}"
        (task_dir / "runs" / run_dir).mkdir(parents=True)
        record = {"dir": run_dir, "role": KIND, "provider": settings["provider"], "model": settings["model"], "effort": settings["effort"],
                  "started": tasks._now(), "finished": None, "pid": None, "send_id": send_id, "message": message, "resumed": settings["cli_session"]}
        prompt, instructions = message, None
        if settings["mode"] == "edit":  # ADR-0046 for Claude, ADR-0047 for Codex: the project's check runs after the turn, on the host.
            project = tasks._projects().get(task["project"], {})
            last = task["runs"][-1] if task["runs"] else {}
            if last.get("role") == CHECK:  # Its result, once, ahead of the keeper's words (ADR-0046); `message` keeps theirs only.
                prompt = _check_note(task_dir, last) + message
                record["check_note"] = last["dir"]
            if settings["provider"] == "codex" and not settings["cli_session"] and project.get("check"):
                # A resumed Codex turn ignores new developer instructions (probe, 2026-10-23), so only the first turn gets them.
                instructions = _instructions(project["check"], project.get("turn_check"))
                settings["turn_check"] = project.get("turn_check")  # What Codex was told, for the commands note.
                (task_dir / "runs" / run_dir / "developer_instructions.md").write_text(instructions, encoding="utf-8")
            record.update(fingerprint=_fingerprint(task), host_check=project.get("check"))  # Taken now: settling never reads projects.json.
        (task_dir / "runs" / run_dir / "prompt.md").write_text(prompt, encoding="utf-8")  # ADR-0024: exactly what goes on stdin.
        task["runs"].append(record)  # Recorded before launch, so a retry after a crash finds it and never sends twice.
        if task["goal"] == "New conversation":
            words = " ".join(message.split())
            task["goal"] = words if len(words) <= 80 else words[:79].rsplit(" ", 1)[0] + "…"
        task["status"] = "running"
        tasks._write(task_dir, task)
        try:
            process = _launch(task, task_dir, record, prompt, instructions)
        except (OSError, LaunchRefused) as exc:
            record.update(finished=tasks._now(), outcome="failed",
                          error=str(exc) if isinstance(exc, LaunchRefused) else f"The {settings['provider']} command could not start: {exc}")
            task["status"] = "done"
            tasks._write(task_dir, task)
            return summary(task)
        with _active_lock:
            _active[session_id] = process  # Tracked before anything else can fail, so quitting always finds and stops it.
        try:
            # The backend that owns the turn: another backend on the same home must not take it for a crash's leftover.
            record.update(pid=process.pid, pid_identity=tasks._identity(process.pid), owner=os.getpid(), owner_identity=tasks._identity(os.getpid()))
            tasks._write(task_dir, task)
        finally:
            # Settles the turn even if that write failed, in this conversation's folder, found now rather than when the turn ends.
            tasks._track(record)
            threading.Thread(target=_wait, args=(task_dir, session_id, process), daemon=True).start()
    cleared = False  # Said in the reply, so a page whose draft was not cleared here saves the cleared draft itself and retries.
    if isinstance(draft_at, int):
        try:
            with tasks._task_lock(task_dir):
                if draft_at >= _draft(task_dir)["at"]:
                    _write_draft(task_dir, "", draft_at)  # Sent: an empty draft at that version, so a late save of the same text is ignored.
                cleared = True
        except OSError:
            pass  # The turn runs and is tracked either way.
    return {**summary(task), "draft_cleared": cleared}


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
        _cancel_requests(record, "stop")  # In the step that marks the stop, so no answer is sent once it has begun (ADR-0045).
        tasks._write(task_dir, task)
    with _active_lock:
        process = _active.get(session_id)
    if process is not None:
        _stop_turn(task_dir, process)
        if process.returncode is not None:  # Otherwise it could not be stopped, which its record says; its watcher settles it.
            _wait(task_dir, session_id, process)
    else:  # A turn this backend did not start, such as one left by a crash.
        _settle(task_dir)
    return summary(tasks._read(task_dir))


def stop_all() -> None:
    """When the backend stops, stop every turn it started (ADR-0041), keeping their output; nothing is resent later."""
    global _quitting
    with _active_lock:
        _quitting = True
        running = list(_active.items())
    for session_id, process in running:
        try:
            with tasks._task_lock(_dir(session_id)):
                task = tasks._read(_dir(session_id))
                task["runs"][-1]["stopping"] = "quit"
                _cancel_requests(task["runs"][-1], "quit")
                tasks._write(_dir(session_id), task)
        except (OSError, ValueError, KeyError, IndexError, SessionError):
            pass  # Its record is unreadable or gone; the process is still stopped.
        task_dir = tasks._home() / "tasks" / session_id
        try:
            _stop_turn(task_dir, process)
            if process.returncode is not None:  # Otherwise it could not be stopped, which its record says; recovery settles it.
                _wait(task_dir, session_id, process)
        except Exception:  # noqa: BLE001 - one conversation that cannot be stopped cleanly must not leave the others running.
            continue


def recover() -> None:
    """At backend start: a turn left running by a crashed backend is stopped by its recorded identity and marked interrupted."""
    for task in _sessions():
        last = task["runs"][-1] if task["runs"] else {}
        if last and not last.get("finished") and not _owned_elsewhere(last):
            _settle(tasks._home() / "tasks" / task["id"])


def _owned_elsewhere(record: dict) -> bool:
    """Whether the backend that started the turn still runs, so the turn is its to finish, not a crash's leftover."""
    owner = record.get("owner")
    return bool(owner and owner != os.getpid() and record.get("owner_identity") and tasks._identity(owner) == record["owner_identity"])


def listing() -> dict:
    return {"sessions": [summary(task) for task in _sessions()], "options": options(), "usage": _usage()}


def _usage() -> list[dict]:
    """Each agent's plan use, to show and never to act on (ADR-0050); a log that cannot be read leaves it unknown, and the list still loads."""
    try:
        return usage.report(usage.codex_limits(*usage.codex_sessions()), usage.claude_limits(tasks._home() / "usage/claude-limits.jsonl"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):  # CODE-3: these logs may be mid-write, or change shape.
        return []


def detail(session_id: str) -> dict:
    task_dir = _dir(session_id)
    return {**summary(tasks._read(task_dir), turns=True), "draft": _draft(task_dir)}


def save_draft(session_id: str, text: str, at: int) -> dict:
    """Keep the keeper's unsent text (ADR-0044); a save older than the stored version, such as one that arrives after its send, is ignored."""
    if not isinstance(text, str) or len(text) > MESSAGE_LIMIT or not isinstance(at, int) or isinstance(at, bool) or at < 0:
        raise SessionError(f"A draft is text of up to {MESSAGE_LIMIT} characters with a version number.")
    task_dir = _dir(session_id)
    with tasks._task_lock(task_dir):
        if at > _draft(task_dir)["at"]:
            _write_draft(task_dir, text, at)
        return _draft(task_dir)


def changes(session_id: str) -> dict:
    """What the session's worktree changed since its base, in the task diff's shape: git read commands only, without the index lock."""
    task = tasks._read(_dir(session_id))
    git = lambda *args: _git(task, *args)  # noqa: E731
    tracked = git("diff", "--no-color", "--no-ext-diff", "--no-textconv", task["base"])
    names = git("diff", "--name-status", "-z", "--no-renames", task["base"])
    if tracked.returncode or names.returncode:
        raise SessionError("Hearth could not read this conversation's changes. Next: check that its worktree still exists.", 409)
    fields = names.stdout.decode("utf-8", errors="replace").split("\0")
    files = [{"path": path, "status": status} for status, path in zip(fields[0::2], fields[1::2]) if path]
    untracked = [name for name in git("ls-files", "--others", "--exclude-standard", "-z", "--", ".", *_mine(task)).stdout
                 .decode("utf-8", errors="replace").split("\0") if name]
    raw = tracked.stdout
    for name in untracked[:UNTRACKED_LIMIT]:
        files.append({"path": name, "status": "?"})
        if len(raw) <= tasks.DIFF_LIMIT:  # Past the limit nothing more is shown, so nothing more is read.
            # Shown by git, never read here: a link shows the path it points to, not the file there.
            raw += git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-index", "--", "/dev/null", name).stdout
    lines = raw.decode("utf-8", errors="replace").splitlines()
    shown = raw if len(raw) <= tasks.DIFF_LIMIT else raw[:tasks.DIFF_LIMIT][: raw[:tasks.DIFF_LIMIT].rfind(b"\n") + 1]
    return {"session": task["id"], "files": files, "untracked_hidden": max(0, len(untracked) - UNTRACKED_LIMIT),
            "diff": shown.decode("utf-8", errors="replace") or None,
            "added": sum(line.startswith("+") and not line.startswith("+++") for line in lines),
            "removed": sum(line.startswith("-") and not line.startswith("---") for line in lines),
            "truncated": len(raw) > tasks.DIFF_LIMIT}


def _git(task: dict, *args: str, check: bool = False, input: bytes | None = None) -> subprocess.CompletedProcess:
    # No optional locks, so an agent's own git add or commit never meets Hearth's lock; no external diff or textconv programs.
    return subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", task["worktree"], *args],
                          capture_output=True, timeout=30, check=check, input=input)


def _mine(task: dict) -> list[str]:
    # Not the agent's work, as for a task's uncommitted files: Hearth's own folder, run caches, and the guidance Hearth copied in.
    return [":(exclude).hearth", ":(exclude,glob)**/__pycache__/**", *[f":(exclude){name}" for name in tasks._copied(task)]]


def open_folder(session_id: str) -> dict:
    """Open the session's own worktree in Finder (ADR-0044): only the folder Hearth made for it, never a path from the request."""
    task = tasks._read(_dir(session_id))
    expected = tasks._home() / "worktrees" / task["project"] / task["id"]
    worktree = Path(task["worktree"])
    if worktree != expected or worktree.is_symlink() or not worktree.is_dir():
        raise SessionError("This conversation's work folder is missing or not the one Hearth made, so Hearth will not open it.", 409)
    try:
        opened = subprocess.run(["open", str(worktree)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SessionError(f"Hearth could not open the work folder: {exc}. It is {worktree}.", 500) from None
    if opened.returncode:
        reason = (opened.stderr or "").strip().splitlines()[-1:] or [f"open exited with {opened.returncode}"]
        raise SessionError(f"Hearth could not open the work folder: {reason[0]} It is {worktree}.", 500)
    return {"opened": str(worktree)}


def publish(session_id: str) -> dict:
    """Commit what the conversation changed, push its branch, and open its pull request, or find the one already open.
    Only the keeper's click starts it, and never during a turn, so the agent's own git work never meets it."""
    task_dir = _dir(session_id)
    with tasks._task_lock(task_dir):
        task = tasks._read(task_dir)
        if task["runs"] and not task["runs"][-1].get("finished"):
            raise SessionError("A turn is still running in this conversation. Next: wait for it, or stop it.", 409)
        branch, title = task["branch"], task["goal"]
        try:
            # Named one by one, as git add fails when an exclusion names an ignored file, such as copied-in guidance.
            names = _git(task, "ls-files", "-z", "--modified", "--deleted", "--others", "--exclude-standard", "--", ".", *_mine(task), check=True).stdout
            if names:
                _git(task, "--literal-pathspecs", "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", check=True, input=names)
            if _git(task, "diff", "--cached", "--quiet").returncode:
                _git(task, "commit", "-q", "-m", title, check=True)
            log = _git(task, "log", "--reverse", "--format=- %s", f"{task['base']}..HEAD", check=True).stdout.decode("utf-8", errors="replace")
        except subprocess.CalledProcessError as exc:
            raise SessionError(f"Hearth could not commit: {_last_line(exc.stderr) or 'git failed'}", 409) from None
        except subprocess.TimeoutExpired:
            raise SessionError("Hearth could not commit: git took longer than 30 seconds.", 409) from None
        if not log:
            raise SessionError("Nothing to commit or push: this conversation has not changed anything yet.", 409)
        leaks = sorted({kind for kind, pattern in tasks.GUARD_PATTERNS if re.search(pattern, f"{title}\n{log}")})
        if leaks:
            raise SessionError(f"Committed, but not pushed: the pull request text would contain {', '.join(leaks)} (CODE-6). Next: push it yourself.", 409)
        # No -u: Hearth never writes the shared repository's config. The push names the branch, so HEAD's state does not matter.
        pushed = _outside(["git", "-C", task["worktree"], "push", "origin", f"HEAD:refs/heads/{branch}"], 120)
        if pushed.returncode:
            raise SessionError(f"Committed, but not pushed: {_last_line(pushed.stderr) or 'git push failed'}", 409)
        found = _outside(["gh", "pr", "view", branch, "--json", "url,state"], 60, task["worktree"])
        state = json.loads(found.stdout) if not found.returncode and found.stdout.strip() else {}
        if state.get("state") == "OPEN":
            url = state["url"]
        else:
            # shortcut: the PR targets the repository's default branch, so a conversation begun on another branch shows its commits too.
            made = _outside(["gh", "pr", "create", "--head", branch, "--title", title, "--body", log], 60, task["worktree"])
            if made.returncode:
                raise SessionError(f"Pushed, but no pull request: {_last_line(made.stderr) or 'gh pr create failed'}", 409)
            url = made.stdout.strip().splitlines()[-1]
        task["pr"] = {"url": url}
        tasks._write(task_dir, task)
        return summary(task)


def _outside(argv: list[str], seconds: int, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run git push or gh, whose failures are the keeper's to read, never Hearth's to raise."""
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=seconds)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]} is not installed." + (" Next: brew install gh, then gh auth login." if argv[0] == "gh" else ""))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{argv[0]} took longer than {seconds} seconds.")


def _last_line(text: str | bytes | None) -> str:
    text = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text or ""
    return (text.strip().splitlines() or [""])[-1]


def summary(task: dict, turns: bool = False) -> dict:
    last = task["runs"][-1] if task["runs"] else None
    running = bool(last and not last.get("finished"))
    replies = [run for run in task["runs"] if run["role"] != CHECK]  # The check Hearth runs after a turn is shown with that turn.
    found = {"id": task["id"], "project": task["project"], "title": task["goal"], "created": task["created"],
             # CLI-5's words: running during a turn or its check, otherwise how the last turn ended; None before the first turn.
             "status": ("running" if task["status"] == "running" else "interrupted") if running else (replies[-1] if replies else {}).get("outcome"),
             "provider": task["session"]["provider"], "mode": task["session"]["mode"], "model": task["session"]["model"],
             "effort": task["session"]["effort"], "resumable": bool(task["session"]["cli_session"]), "turn_count": len(replies),
             "updated": (last or {}).get("finished") or (last or {}).get("started") or task["created"], "worktree": task["worktree"],
             "pr": (task.get("pr") or {}).get("url")}
    if turns:
        if found["mode"] == "full":
            check = None
            description = ("Full access: your own Claude setup. It runs any command as you, without asking and without a sandbox."
                           if found["provider"] == "claude" else "Full access: Codex runs any command as you, without asking and without a sandbox.")
        elif found["provider"] == "claude":
            check = tasks._projects().get(task["project"], {}).get("turn_check") if found["mode"] == "edit" else None
            description = ("Your own Claude setup, inside this worktree. A shell command asks you first and runs in Claude Code's sandbox, "
                           "except read-only ones." if found["mode"] == "edit" else "Read and search files only. Shell commands are unavailable.")
            description += " Without asking:" if check else ""
        else:
            check = None
            sandbox = "workspace-write" if found["mode"] == "edit" else "read-only"
            description = f"Shell commands run within Codex's {sandbox} sandbox."
        found["commands"] = {"description": description, "check": check}
        if found["mode"] == "edit":
            found["commands"]["host_check"] = tasks._projects().get(task["project"], {}).get("check")
        if found["provider"] == "codex" and found["mode"] == "edit":
            # What Codex was told at its first turn, which later edits to projects.json do not reach (ADR-0047).
            found["commands"]["turn_check"] = task["session"].get("turn_check")
        found["turns"] = []
        for run in task["runs"]:
            shown = {key: run.get(key) for key in ("dir", "message", "model", "effort", "started", "finished", "outcome", "exit_code",
                                                   "model_used", "sandbox_used", "warnings", "error", "session_id", "resumed", "stopped_by",
                                                   "left_running", "left_in_folder", "cleanup_unchecked", "denied", "model_refused", "approvals",
                                                   "claude_code_version")}
            if run["role"] != CHECK:
                found["turns"].append({**shown, "check": None})
            elif found["turns"]:
                found["turns"][-1]["check"] = {**shown, "command": run.get("command"), "tail": _tail(tasks._home() / "tasks" / task["id"], run)}
    return found


def _tail(task_dir: Path, record: dict) -> str | None:
    """The last lines of a finished check's output, as its summary gives them; None while it runs."""
    output = task_dir / "runs" / record["dir"] / "output.txt"
    if not record.get("finished") or not output.exists():
        return None
    return "\n".join(output.read_text(encoding="utf-8", errors="replace").splitlines()[-SUMMARY_LINES:])


def _launch(task: dict, task_dir: Path, record: dict, message: str, instructions: str | None = None) -> subprocess.Popen:
    settings, project = task["session"], tasks._projects()[task["project"]]
    full = _full_setup(settings)
    table = FULL_ACCESS if settings["mode"] == "full" else getattr(tasks, MODES[settings["mode"]])
    template = CONVERSATION[settings["provider"]] if full else table[settings["provider"]]
    if full:
        if excluded := _excluded_commands(Path(task["worktree"])):
            raise LaunchRefused(f"sandbox_excluded_commands: {excluded} sets sandbox.excludedCommands, and Claude Code runs an excluded "
                                "command outside its sandbox without asking. Next: remove that setting, then send again.")
        guard = _git_guard(task)
        if len(guard) > GUARD_LIMIT:
            raise LaunchRefused(f"git_guard_too_large: {len(guard)} paths in the repository's .git would need protecting, over the limit "
                                f"of {GUARD_LIMIT}. Next: run git gc in the project's folder, then send again.")
        claude_settings = json.dumps({"sandbox": {**SANDBOX, "filesystem": {"denyWrite": guard}}})
        if not project.get("turn_check"):  # ADR-0046: only the check that works inside the sandbox runs without asking.
            template = [part for part in template if part not in ("--allowedTools", "Bash({check} *)")]
    else:
        claude_settings = tasks._claude_settings(Path(task["worktree"]))
    argv = tasks._argv(template, settings["provider"], message, (project.get("turn_check") if full else project.get("check")) or "true",
                       settings["model"], settings["effort"], settings=claude_settings)
    if instructions:
        argv[argv.index("--json") + 1:argv.index("--json") + 1] = ["-c", f"developer_instructions={json.dumps(instructions)}"]  # A TOML string.
    argv = _resumable(argv, settings["provider"], settings["cli_session"], asks=full)
    record["argv"] = argv  # It holds no message text, which goes on stdin.
    run_dir = task_dir / "runs" / record["dir"]
    events, errors = (run_dir / "events.jsonl").open("w", encoding="utf-8"), (run_dir / "stderr.txt").open("w", encoding="utf-8")
    record["marker"] = tasks._new_marker()
    record["descendants"] = str(run_dir / "descendants")
    env = {**os.environ, **tasks.NO_BYTECODE, "HEARTH_TASK": task["id"], tasks.RUN_MARKER: record["marker"]}
    if not full and settings["mode"] != "full":  # Full access is the keeper's own setup too, memory included.
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    if settings["provider"] == "codex":
        env["CODEX_HOME"] = str(tasks._codex_home(task_dir))
    try:
        process = subprocess.Popen(argv, cwd=task["worktree"], stdin=subprocess.PIPE, stdout=subprocess.PIPE if full else events,
                                   stderr=errors, text=True, encoding="utf-8", errors="replace", start_new_session=True, env=env)
    finally:
        events.close()
        errors.close()
    if not full:
        process.stdin.write(message)
        process.stdin.close()
        return process
    # ADR-0045: the turn's stdin stays open for answers to its command requests, and its stdout is read here, line by line.
    os.set_blocking(process.stdin.fileno(), False)
    stream = {"process": process, "pending": {}, "timers": {}, "init": f"hearth-{os.urandom(4).hex()}"}
    with _active_lock:
        _streams[task["id"]] = stream
    stream["reader"] = threading.Thread(target=_read_stream, args=(task_dir, task["id"], run_dir / "events.jsonl", stream), daemon=True)
    stream["reader"].start()

    def begin() -> None:  # Its own thread: a long message waits for Claude Code to start reading, and send() does not wait with it.
        opening = ({"type": "control_request", "request_id": stream["init"], "request": {"subtype": "initialize", "hooks": None}},
                   {"type": "user", "message": {"role": "user", "content": message}, "parent_tool_use_id": None})
        if not all(_tell(process, line, INITIALIZE_SECONDS) for line in opening) and process.poll() is None:
            _protocol_changed(task_dir, record["dir"], process, "Claude Code did not take the message")
    threading.Thread(target=begin, daemon=True).start()
    return process


def _tell(process: subprocess.Popen, message: dict, seconds: float = WRITE_SECONDS) -> bool:
    """Write one line to a stream-json turn's stdin, without waiting long on an agent that has stopped reading; False if it did
    not all go, after which the stream cannot be trusted and the caller stops the turn."""
    data, end = (json.dumps(message) + "\n").encode(), time.monotonic() + seconds
    with _stdin_lock:
        try:
            while data:
                try:
                    data = data[os.write(process.stdin.fileno(), data):]
                except BlockingIOError:
                    if time.monotonic() > end:
                        return False
                    time.sleep(0.02)
        except (OSError, ValueError):  # The agent has exited, or its stdin is closed.
            return False
    return True


def _read_stream(task_dir: Path, session_id: str, events_path: Path, stream: dict) -> None:
    """Copy the turn's stdout to its events file as it arrives, unchanged, and act on what Claude Code asks its host."""
    process, ready, run = stream["process"], threading.Event(), events_path.parent.name
    watchdog = stream["watchdog"] = threading.Timer(
        INITIALIZE_SECONDS, lambda: ready.is_set() or _protocol_changed(task_dir, run, process, "Claude Code did not answer initialize"))
    watchdog.daemon = True
    watchdog.start()
    try:
        with events_path.open("a", encoding="utf-8") as events:
            for line in process.stdout:
                events.write(line)
                events.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "control_response":
                    reply = event.get("response") if isinstance(event.get("response"), dict) else {}
                    if reply.get("request_id") == stream["init"]:
                        ready.set()
                        if reply.get("subtype") != "success":
                            _protocol_changed(task_dir, run, process, "Claude Code refused initialize")
                elif event.get("type") == "control_request":
                    # Not on this thread: it waits for the conversation's lock, and Claude Code's output must keep being read meanwhile.
                    threading.Thread(target=_asked, args=(task_dir, session_id, run, stream, event), daemon=True).start()
                elif event.get("type") == "result":
                    with _stdin_lock:  # The turn is over: with its input ended, the process exits.
                        process.stdin.close()
    except (OSError, ValueError):
        pass  # The pipe or the events file went away; the turn's watcher settles it from its exit.
    finally:
        watchdog.cancel()
        process.stdout.close()


def _asked(task_dir: Path, session_id: str, run: str, stream: dict, event: dict) -> None:
    """One control request from Claude Code: a Bash command waits for the keeper, anything else is refused and recorded (ADR-0045)."""
    request = event.get("request") if isinstance(event.get("request"), dict) else {}
    request_id, tool, asked = event.get("request_id"), request.get("tool_name"), request.get("input")
    if request.get("subtype") != "can_use_tool":
        return _protocol_changed(task_dir, run, stream["process"], f"Claude Code sent an unknown control request, {str(request.get('subtype'))[:40]}")
    if not (isinstance(request_id, str) and request_id and isinstance(tool, str) and isinstance(asked, dict)):
        return _protocol_changed(task_dir, run, stream["process"], "Claude Code sent a command request without its ID, tool or input")
    entry = {"request_id": request_id, "tool_use_id": request.get("tool_use_id"), "tool": tool,
             "command": asked.get("command") if tool == "Bash" else None, "description": asked.get("description") or request.get("description"),
             "asked": tasks._now(), "decision": None, "by": None, "reason": None, "decided": None}
    if tool == "SandboxNetworkAccess":
        refusal = f"a network host, {str(asked.get('host'))[:120]}"
    elif tool != "Bash" or not isinstance(entry["command"], str):
        path = request.get("blocked_path") or asked.get("file_path")
        refusal = f"{tool} on {str(path)[:200]}" if path else f"a tool other than Bash, {tool[:80]}"
    else:
        refusal = None
    with tasks._task_lock(task_dir):
        task = tasks._read(task_dir)
        record = next((item for item in task["runs"] if item["dir"] == run), None)
        if record is None:
            return
        if record.get("protocol_error"):  # Requests are handled on their own threads, so one can reach here after a later one broke the protocol.
            entry.update(decision="refused", by="policy", reason="claude_protocol_changed", decided=tasks._now())
            _tell(stream["process"], _reply(request_id, False, None, "policy"))
        elif record.get("finished") or record.get("stopping"):
            entry.update(decision="cancelled", by=STOPPED.get(record.get("stopping"), "crash"), decided=tasks._now())  # Never answered.
        elif refusal:
            entry.update(decision="refused", by="policy", reason=refusal, decided=tasks._now())
            _tell(stream["process"], _reply(request_id, False, None, "policy"))
        else:
            stream["pending"][request_id] = asked
            timer = stream["timers"][request_id] = threading.Timer(EXPIRY_SECONDS, _expire, args=(session_id, request_id))
            timer.daemon = True
            timer.start()
        record.setdefault("approvals", []).append(entry)
        tasks._write(task_dir, task)


def _reply(request_id: str, allow: bool, asked: dict | None, by: str) -> dict:
    # Allow returns the input as it came: Hearth never edits a command, adds a permission rule, or echoes Claude's suggestions.
    body = {"behavior": "allow", "updatedInput": asked} if allow else {"behavior": "deny", "message": REFUSALS[by]}
    return {"type": "control_response", "response": {"subtype": "success", "request_id": request_id, "response": body}}


def answer(session_id: str, request_id: str, allow: bool) -> dict:
    """The keeper's Allow once or Deny for one pending command; the same answer, repeated, returns what was recorded."""
    return _decide(session_id, request_id, allow, "keeper")


def _expire(session_id: str, request_id: str) -> None:
    try:
        _decide(session_id, request_id, False, "expiry")
    except (SessionError, OSError, ValueError, KeyError, IndexError):
        pass  # Answered, cancelled, or its conversation is gone.


def _decide(session_id: str, request_id: str, allow: bool, by: str) -> dict:
    """Record a decision and send it to Claude Code in one locked step, only while the request can still be answered (ADR-0045)."""
    task_dir = _dir(session_id)
    with tasks._task_lock(task_dir):
        task = tasks._read(task_dir)
        entry = next((item for run in task["runs"] for item in run.get("approvals") or [] if item.get("request_id") == request_id), None)
        if entry is None or not request_id:
            raise SessionError("That request is not in this conversation.", 404)
        if entry["decision"]:
            return summary(task, turns=True)
        record = task["runs"][-1]
        with _active_lock:
            stream = _streams.get(session_id)
        if (entry not in (record.get("approvals") or []) or record.get("finished") or record.get("stopping") or record.get("protocol_error")
                or stream is None or request_id not in stream["pending"]):
            raise SessionError("This request can no longer be answered: its turn has ended or is stopping.", 409)
        asked = stream["pending"].pop(request_id)
        stream["timers"].pop(request_id).cancel()
        entry.update(decision="allowed" if allow else "expired" if by == "expiry" else "denied", by=by, decided=tasks._now())
        tasks._write(task_dir, task)  # Recorded first: an answer that cannot be delivered stays on the record, with that note.
        delivered = _tell(stream["process"], _reply(request_id, allow, asked, by))
        if not delivered:
            entry["reason"] = "The answer could not be delivered: the agent was no longer reading."
            tasks._write(task_dir, task)
        found = summary(task, turns=True)
    if not delivered and stream["process"].poll() is None:  # Part of the answer may have gone, so nothing after it can be trusted.
        _protocol_changed(task_dir, record["dir"], stream["process"], "Claude Code stopped reading Hearth's answers")
    return found


def _cancel_requests(record: dict, by: str) -> None:
    for entry in record.get("approvals") or []:
        if not entry.get("decision"):
            entry.update(decision="cancelled", by=by, decided=tasks._now())


def _protocol_changed(task_dir: Path, run: str, process: subprocess.Popen, why: str) -> None:
    """ADR-0045: Claude Code did not speak the protocol Hearth knows, so the turn stops and says so; it never runs without approvals.
    `run` is the turn it happened in: a late call, after that turn ended, must not touch the conversation's next one."""
    refused = []
    try:
        with tasks._task_lock(task_dir):
            task = tasks._read(task_dir)
            record = task["runs"][-1]
            if record["dir"] != run or record.get("finished") or record.get("protocol_error"):
                return
            record["protocol_error"] = why  # From here no answer is accepted, so the refusals can be sent after the lock.
            for entry in record.get("approvals") or []:
                if not entry.get("decision"):
                    entry.update(decision="refused", by="policy", reason="claude_protocol_changed", decided=tasks._now())
                    refused.append(entry["request_id"])
            tasks._write(task_dir, task)
    except (OSError, ValueError, KeyError, IndexError):
        pass  # Its record is unreadable or gone; the turn is still stopped.
    for request_id in refused:
        _tell(process, _reply(request_id, False, None, "policy"))
    _stop_turn(task_dir, process)


def _excluded_commands(worktree: Path) -> str | None:
    """The first settings file Claude Code will load that names sandbox.excludedCommands; on 2026-10-20 such a command ran outside the
    sandbox with no request, and Claude Code adds lists from every settings file together, so Hearth's own settings cannot empty it."""
    for path in (Path.home() / ".claude/settings.json", Path.home() / ".claude/settings.local.json",
                 worktree / ".claude/settings.json", worktree / ".claude/settings.local.json"):
        try:
            found = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # Absent, or one Claude Code ignores too.
        sandbox = found.get("sandbox") if isinstance(found, dict) else None
        if isinstance(sandbox, dict) and sandbox.get("excludedCommands"):
            return str(path)
    return None


def _resumable(argv: list[str], provider: str, cli_session: str | None, asks: bool = False) -> list[str]:
    """The table's invocation, made to stream partial text and to continue the CLI's own session after the first turn."""
    argv = list(argv)
    if provider == "claude":
        argv.insert(argv.index("--verbose") + 1, "--include-partial-messages")  # Text as it arrives, kept if the turn is stopped.
        if cli_session:
            argv[1:1] = ["--resume", cli_session]
        if asks:  # ADR-0045: permission prompts come to Hearth as control requests; without the second flag they are refused unseen.
            argv += ["--input-format", "stream-json", "--permission-prompt-tool", "stdio"]
    elif provider == "codex" and cli_session:
        # `codex exec resume` has no --sandbox flag; the same mode goes through -c, which held on 2026-10-13's real runs.
        index = argv.index("--sandbox")
        argv[index:index + 2] = ["-c", f'sandbox_mode="{argv[index + 1]}"']
        argv[1:2] = ["exec", "resume", cli_session]
    return argv


def _wait(task_dir: Path, session_id: str, process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            with tasks._task_lock(task_dir):  # Marked before stopping, as Stop is, so no answer is sent to a turn that timed out.
                task = tasks._read(task_dir)
                task["runs"][-1]["stopping"] = "timeout"
                _cancel_requests(task["runs"][-1], "timeout")
                tasks._write(task_dir, task)
        except (OSError, ValueError, KeyError, IndexError):
            pass  # Its record is unreadable or gone; the process is still stopped.
        _stop_turn(task_dir, process)
        process.wait()
    with _active_lock:
        if _active.get(session_id) is process:
            del _active[session_id]
        stream = _streams.get(session_id)
        if stream is not None and stream["process"] is process:
            del _streams[session_id]
        else:
            stream = None
    if stream is not None:
        for timer in (*stream["timers"].values(), *([stream["watchdog"]] if "watchdog" in stream else [])):
            timer.cancel()
        # So the last events are on disk before the turn is recorded; bounded, since a command the agent left running may hold the pipe.
        stream["reader"].join(timeout=5)
        with _stdin_lock:
            process.stdin.close()
    _settle(task_dir, process.returncode)


def _stop_turn(task_dir: Path, process: subprocess.Popen) -> None:
    """Stop the running turn and everything it started, and keep on its record whatever could not be stopped or checked."""
    try:
        task = tasks._read(task_dir)
        record, worktree = task["runs"][-1], task["worktree"]
    except (OSError, ValueError, KeyError, IndexError):
        record, worktree = {}, ""  # Its record is unreadable or gone; the process is still stopped.
    cleanup = tasks._stop(process, record, worktree)
    if not any(cleanup.values()):
        return
    try:
        with tasks._task_lock(task_dir):
            task = tasks._read(task_dir)
            tasks.note_cleanup(task["runs"][-1], cleanup)
            tasks._write(task_dir, task)
    except (OSError, ValueError, KeyError, IndexError):
        pass  # Its record is unreadable or gone, so there is nowhere to say it.


def _settle(task_dir: Path, exit_code: int | None = None) -> None:
    """Record how the last turn ended, once: from its own exit, or, for a turn no longer owned, by stopping what is left."""
    if not (task_dir / "task.json").is_file():
        return  # The conversation's record is gone, so there is nothing left to record the end in.
    with tasks._task_lock(task_dir):
        task = tasks._read(task_dir)
        if not task["runs"] or task["runs"][-1].get("finished"):
            return
        record = task["runs"][-1]
        owned = exit_code is not None
        if not owned and record.get("pid"):
            tasks.note_cleanup(record, tasks._stop_pid(record, task["worktree"])[1])  # The leader only while it still is that turn.
        if record["role"] == CHECK:
            outcome = "cancelled" if record.get("stopping") else "interrupted" if not owned else "done" if exit_code == 0 else "failed"
            record.update(finished=tasks._now(), exit_code=exit_code, outcome=outcome, stopped_by=record.pop("stopping", None))
            task["status"] = "done"
            tasks._write(task_dir, task)
            return
        run_dir = task_dir / "runs" / record["dir"]
        text = (run_dir / "events.jsonl").read_text(encoding="utf-8", errors="replace") if (run_dir / "events.jsonl").exists() else ""
        parsed = tasks.parse_events(record["provider"], text)
        errors = (run_dir / "stderr.txt").read_text(encoding="utf-8", errors="replace") if (run_dir / "stderr.txt").exists() else ""
        _cancel_requests(record, STOPPED.get(record.get("stopping"), "crash"))  # Whatever was still waiting was never answered.
        if record.get("protocol_error"):
            outcome = "failed"
        elif record.get("stopping"):
            outcome = "cancelled"  # CLI-5's word for work the keeper stopped, or quitting did.
        elif not owned:
            outcome = "interrupted"
        elif exit_code != 0 or parsed["error"]:
            outcome = "failed"
        else:
            outcome = "done"
        record.update(finished=tasks._now(), exit_code=exit_code, outcome=outcome, session_id=parsed["session_id"],
                      warnings=sorted(set(WARNINGS.findall(errors))) or None, stopped_by=record.pop("stopping", None))
        version = re.search(r'"claude_code_version":\s*"([^"]{1,40})"', text)
        if version:
            record["claude_code_version"] = version.group(1)
        if record.get("protocol_error"):
            record["error"] = f"claude_protocol_changed: {record['protocol_error']}. Next: update Hearth, or start a read-only conversation."
        elif outcome == "failed":
            record["error"] = parsed["error"] or (errors.strip().splitlines() or [f"Exit code {exit_code}"])[-1][:500]
            record["model_refused"] = "model_not_found" in text or "unrecognized_model" in errors  # Each CLI's word, seen on 2026-11-06.
        if record["provider"] == "codex":
            record["model_used"], record["sandbox_used"] = _codex_context(task_dir, parsed["session_id"])
        else:
            record["model_used"] = parsed["model"]
        record["denied"] = _denied(text)
        if parsed["session_id"] and not task["session"]["cli_session"]:
            task["session"]["cli_session"] = parsed["session_id"]
        task["status"] = "done"  # Between turns a session is ready, never waiting or queued, so an update is not refused.
        check = record.get("host_check") if outcome == "done" else None
        after = _fingerprint(task) if check else None
        if check and (after is None or after != record["fingerprint"]):  # When Git cannot say, the check runs.
            _start_check(task, task_dir, check)  # Recorded as running in this same lock, so no send slips in before it.
        tasks._write(task_dir, task)


def _start_check(task: dict, task_dir: Path, check: str) -> None:
    """ADR-0047: run the project's check after a Codex turn that changed the worktree, outside the sandbox, launched like a
    turn (its own group, the run's marker, its identity, the timeout) so Stop, Quit, the timeout and recovery end it as one."""
    run_dir = f"{len(task['runs']) + 1:02d}-{CHECK}"
    (task_dir / "runs" / run_dir).mkdir(parents=True)
    record = {"dir": run_dir, "role": CHECK, "provider": "hearth", "command": check, "started": tasks._now(), "finished": None,
              "pid": None, "marker": tasks._new_marker(), "descendants": str(task_dir / "runs" / run_dir / "descendants")}
    task["runs"].append(record)
    task["status"] = "running"
    tasks._write(task_dir, task)  # Recorded before launch, as a turn is: a crash before it starts leaves an interrupted check, not none.
    # As a task's check gets it: without HEARTH_TASK, which marks an agent's shell and stops Hearth's own suite (agent_check) from
    # starting a backend. Keeper-only commands then no longer refuse inside the check, as in a task's check.
    env = {**os.environ, **tasks.NO_BYTECODE, tasks.RUN_MARKER: record["marker"]}
    with _active_lock:
        if _quitting:  # stop_all has already looked, so a check started now would outlive the backend.
            record.update(finished=tasks._now(), exit_code=None, outcome="cancelled", stopped_by="quit")
            task["status"] = "done"
            return
        try:
            with (task_dir / "runs" / run_dir / "output.txt").open("w", encoding="utf-8") as output:
                process = subprocess.Popen(["/bin/sh", "-c", check], cwd=task["worktree"], stdin=subprocess.DEVNULL, stdout=output,
                                           stderr=subprocess.STDOUT, start_new_session=True, env=env)
        except OSError as exc:
            record.update(finished=tasks._now(), exit_code=None, outcome="failed", error=f"The check could not start: {exc}")
            task["status"] = "done"
            return
        _active[task["id"]] = process
    record.update(pid=process.pid, pid_identity=tasks._identity(process.pid), owner=os.getpid(), owner_identity=tasks._identity(os.getpid()))
    tasks._track(record)  # /bin/sh hides its environment on macOS, so what it starts is known by this record after a crash.
    threading.Thread(target=_wait, args=(task_dir, task["id"], process), daemon=True).start()


def _check_note(task_dir: Path, record: dict) -> str:
    """The labelled summary of the check Hearth ran after the last turn, which opens the next message."""
    if record.get("outcome") in ("done", "failed") and record.get("exit_code") is not None:
        result = f"It exited with code {record['exit_code']}. The last lines of its output:\n" + (_tail(task_dir, record) or "")
    else:
        result = "It did not finish, so there is no result."
    return (f"[Hearth] After your last turn, Hearth ran the project's check, `{record['command']}`, outside your sandbox. {result}\n"
            "[End of Hearth's note. The keeper's message follows.]\n\n")


def _instructions(check: str, turn_check: str | None) -> str:
    """What Codex is told about checks, as developer instructions on a conversation's first turn (ADR-0047)."""
    told = (f"For your own checks during a turn, run `{turn_check}`, adding arguments at the end if you need fewer tests. "
            if turn_check else "")
    return (f"{told}Hearth runs the project's full check, `{check}`, after each of your turns that changes files, outside your sandbox, "
            "because it may need what your sandbox does not allow. Do not run it yourself. Its result opens the keeper's next message.")


def _full_setup(settings: dict) -> bool:
    return settings["provider"] == "claude" and settings["mode"] == "edit"


def _config_changed(task: dict) -> str | None:
    """The first Claude configuration path whose contents or mode on disk differ from the base commit, ignored files included;
    when Hearth cannot say, the paths themselves, so the turn is refused rather than run on unknown configuration. It reads the
    files, not Git's index, so an index flag such as skip-worktree or assume-unchanged cannot hide a change."""
    worktree = Path(task["worktree"])
    try:
        base = {}
        for entry in _git(task, "ls-tree", "-r", "-z", task["base"], "--", *AGENT_CONFIG, check=True).stdout.split(b"\0"):
            if entry:
                meta, name = entry.split(b"\t", 1)
                mode, _, blob = meta.split(b" ")
                base[os.fsdecode(name)] = (mode.decode(), blob.decode())
        disk, files, pending = {}, [], [worktree / name for name in AGENT_CONFIG]
        while pending:
            path = pending.pop()
            name = path.relative_to(worktree).as_posix()
            if path.is_symlink():  # Git stores a link as a blob of its target.
                target = os.fsencode(os.readlink(path))
                disk[name] = ("120000", _git(task, "hash-object", "--stdin", check=True, input=target).stdout.decode().strip())
            elif path.is_dir():
                pending += path.iterdir()
            elif path.is_file():
                files.append(name)
                disk[name] = ("100755" if path.stat().st_mode & 0o100 else "100644", None)
            elif os.path.lexists(path):
                disk[name] = ("other", None)
        if files:  # Raw bytes, no filters: attributes in the worktree are the agent's to change.
            hashes = _git(task, "hash-object", "--no-filters", "--stdin-paths", check=True, input="\n".join(files).encode()).stdout.split()
            for name, blob in zip(files, hashes, strict=True):
                disk[name] = (disk[name][0], blob.decode())
    except (OSError, subprocess.SubprocessError, ValueError):
        return " or ".join(AGENT_CONFIG)
    return next((name for name in sorted(base.keys() | disk.keys()) if base.get(name) != disk.get(name)), None)


def _git_guard(task: dict) -> list[str]:
    """ADR-0046: the paths in the repository's shared .git that an approved command must not write, named one by one because
    the sandbox cannot allow a path back inside a denied folder: everything that exists when the turn starts except this
    conversation's own branch and worktree folder. New loose objects and new ref names stay writable."""
    def where(flag: str) -> Path:
        return Path(_git(task, "rev-parse", "--path-format=absolute", flag, check=True).stdout.decode().strip()).resolve()
    shared, own = where("--git-common-dir"), where("--git-dir")
    deny = {shared / name for name in GIT_ROOT_NAMES} | {shared / "objects/pack", shared / "objects/info", shared / "logs/HEAD"}
    deny |= {path for path in shared.iterdir() if path.name not in ("objects", "refs", "logs", "worktrees")}
    deny |= {shared / f"{path.relative_to(shared)}.lock" for path in set(deny)}
    deny |= {path for folder in (shared / "objects").iterdir() if len(folder.name) == 2 and folder.is_dir() for path in folder.iterdir()}
    refs = _git(task, "for-each-ref", "--format=%(refname)", "refs/heads", check=True).stdout.decode().split()
    for ref in (*(ref for ref in refs if ref != f"refs/heads/{task['branch']}"), *GIT_REF_AREAS):
        deny |= {shared / ref, shared / f"{ref}.lock", shared / "logs" / ref, shared / "logs" / f"{ref}.lock"}
    if (shared / "worktrees").is_dir():
        deny |= {path for path in (shared / "worktrees").iterdir() if path != own}
    return sorted(map(str, deny))


def _denied(text: str) -> list[str] | None:
    """What Claude Code refused in a turn because nobody could approve it, from its result line: each tool with the command,
    path, or URL it asked for, once."""
    found = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            for item in event.get("permission_denials") or []:
                if isinstance(item, dict) and item.get("tool_name"):
                    asked = item.get("tool_input") if isinstance(item.get("tool_input"), dict) else {}
                    what = asked.get("command") or asked.get("file_path") or asked.get("url") or asked.get("query")
                    found.append(f"{item['tool_name']} ({str(what)[:120]})" if what else item["tool_name"])
    return list(dict.fromkeys(found)) or None


def _fingerprint(task: dict) -> str | None:
    """HEAD, the tracked diff against the base, and every untracked file's name and content, without Hearth's own files;
    None when Git cannot say."""
    mine = _mine(task)
    try:
        digest = hashlib.sha256()
        for args in (("rev-parse", "HEAD"), ("diff", "--binary", "--no-ext-diff", "--no-textconv", task["base"], "--", ".", *mine)):
            digest.update(_git(task, *args, check=True).stdout)
        names = [name for name in _git(task, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", *mine, check=True)
                 .stdout.split(b"\0") if name]
        for name in names:
            path = Path(task["worktree"]) / os.fsdecode(name)
            digest.update(name + b"\0" + (os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes() if path.is_file() else b""))
        return digest.hexdigest()
    except (OSError, subprocess.SubprocessError):
        return None


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


def _draft(task_dir: Path) -> dict:
    try:
        found = json.loads((task_dir / "draft.json").read_text(encoding="utf-8"))
        return {"text": str(found["text"]), "at": int(found["at"])}
    except (OSError, ValueError, KeyError, TypeError):
        return {"text": "", "at": 0}


def _write_draft(task_dir: Path, text: str, at: int) -> None:
    staged = task_dir / "draft.json.partial"
    staged.write_text(json.dumps({"text": text, "at": at}), encoding="utf-8")
    os.replace(staged, task_dir / "draft.json")


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
