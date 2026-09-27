"""One agent, one worktree: start a headless provider run and keep its receipts."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import usage

# Implementer invocations, confirmed against each installed version (see the workbench specification's Run table).
# "{options}" becomes the model and effort flags, "{check}" the project's check command,
# and "{prompt}" is for a CLI that cannot read the prompt on standard input.
PROVIDERS = {
    "claude": ["claude", "{options}", "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits",
               "--allowedTools", "Bash({check} *)"],
    "codex": ["codex", "exec", "{options}", "--json", "--sandbox", "workspace-write", "-"],
    "antigravity": ["agy", "{options}", "--output-format", "stream-json", "--mode", "accept-edits", "-p", "{prompt}"],
}
# Ignored files a worktree lacks but an agent needs; tracked files are already in every worktree.
GUIDANCE = ["AGENTS.md", "CLAUDE.md", "CODING_REQUIREMENTS.md", "CONTEXT.md", "docs/standards"]
HEADROOM_LIMIT = 90
PROMPT = """# Task {id}

Goal: {goal}

Work only in this directory, and follow AGENTS.md if it exists.
Run tests only with `{check}`, adding arguments at the end if you need fewer tests, such as `-k NAME`; other forms are refused.
Do not commit; Hearth commits a checkpoint after this run and then runs the project's check.
Save anything meant for the person, such as a page or an image, in .hearth/artifacts/.
End with what changed, which checks you ran and their results, and any open questions.
"""


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    parser = subcommands.add_parser("task", help="Run a coding agent on a project in its own Git worktree.")
    actions = parser.add_subparsers(dest="task_command", required=True)
    new = actions.add_parser("new", help="Create a task worktree and run one agent on the goal.")
    new.add_argument("project", help="A project name from ~/.hearth/projects.json.")
    new.add_argument("goal")
    new.add_argument("--agent", choices=sorted(PROVIDERS), help="Provider to run. Defaults to the project's first provider.")
    new.add_argument("--model", help="Model name passed to the provider CLI.")
    new.add_argument("--effort", help="Reasoning effort passed to the provider CLI.")
    new.add_argument("--timeout", type=int, default=1800, help="Seconds before the run is stopped. Defaults to 1800.")
    new.add_argument("--force", action="store_true", help=f"Run even when the provider reports {HEADROOM_LIMIT}%% use or more.")
    listing = actions.add_parser("list", help="List tasks, newest first.")
    listing.add_argument("--json", action="store_true", help="Print one JSON list.")
    show = actions.add_parser("show", help="Show one task and the commands to review and publish it.")
    show.add_argument("id")
    show.add_argument("--json", action="store_true", help="Print the task record as JSON.")
    discard = actions.add_parser("discard", help="Preview, or with --apply remove, a task's worktree and branch.")
    discard.add_argument("id")
    discard.add_argument("--apply", action="store_true", help="Remove the worktree and branch; the task directory stays as a receipt.")


def run(args: argparse.Namespace) -> int:
    if args.task_command == "new":
        return _new(args)
    if args.task_command == "list":
        tasks = [_read(path.parent) for path in sorted(_home().glob("tasks/*/task.json"), reverse=True)]
        if args.json:
            print(json.dumps(tasks))
        for task in [] if args.json else tasks:
            print(f"{task['id']}  {task['status']:<11}  {task['project']:<12}  {task['goal'][:60]}")
        if not tasks and not args.json:
            print('Next: hearth task new <project> "<goal>"', file=sys.stderr)
        return 0
    task_dir = _home() / "tasks" / args.id
    if not (task_dir / "task.json").exists():
        print(f"No task {args.id}.\nNext: hearth task list", file=sys.stderr)
        return 1
    task = _read(task_dir)
    if args.task_command == "show":
        _show(task, task_dir, args.json)
        return 0
    return _discard(task, task_dir, args.apply)


def parse_events(provider: str, text: str) -> dict:
    """Reduce a provider's event stream to final text, session ID, usage, and a named error."""
    result = {"final": "", "session_id": None, "usage": None, "error": None}
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if provider == "claude":
            result["session_id"] = event.get("session_id") or result["session_id"]
            if event.get("type") == "result":
                result.update(final=event.get("result") or "", usage=event.get("usage"))
                if event.get("is_error"):
                    result["error"] = "provider_error"
        elif provider == "codex":
            item = event.get("item") or {}
            if event.get("type") == "thread.started":
                result["session_id"] = event["thread_id"]
            elif item.get("type") == "agent_message":
                result["final"] = item["text"]
            elif event.get("type") == "turn.completed":
                result["usage"] = event.get("usage")
            elif event.get("type") in ("turn.failed", "error"):
                result["error"] = "provider_error"
        elif provider == "antigravity" and event.get("event") == "result":
            outcome = event["result"]
            result.update(final=outcome.get("response") or "", session_id=outcome.get("conversation_id"), usage=outcome.get("usage"))
            if outcome.get("status") != "SUCCESS":
                result["error"] = "provider_error"
    if result["error"] and "authenticate" in result["final"].lower():
        result["error"] = "auth_expired"
    return result


def _new(args: argparse.Namespace) -> int:
    projects = json.loads((_home() / "projects.json").read_text(encoding="utf-8"))
    if args.project not in projects:
        print(f"No project {args.project} in ~/.hearth/projects.json.\nNext: add it there, or use one of: {', '.join(projects)}", file=sys.stderr)
        return 1
    project = projects[args.project]
    provider = args.agent or project["providers"][0]
    if provider not in project["providers"]:
        print(f"{args.project} does not allow {provider}.\nNext: add it to the project's providers, or choose one of: {', '.join(project['providers'])}", file=sys.stderr)
        return 1
    spent = [row for row in usage.report(usage.codex_limits(Path.home() / ".codex/sessions"), usage.claude_limits(_home() / "usage/claude-limits.jsonl"))
             if row["provider"] == provider and max(row["five_hour"] or 0, row["week"] or 0) >= HEADROOM_LIMIT]
    if spent and not args.force:
        print(f"{provider} is at {HEADROOM_LIMIT}% or more of a plan limit.\nNext: hearth usage, then pick another --agent or add --force", file=sys.stderr)
        return 1

    repo = Path(project["path"]).expanduser()
    task_id = time.strftime("%Y%m%d-%H%M%S")
    task_dir = _home() / "tasks" / task_id
    worktree = _home() / "worktrees" / args.project / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "brief.md").write_text(f"# {args.goal}\n", encoding="utf-8")
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "worktree", "add", "-q", "-b", f"hearth/{task_id}", str(worktree), base)
    copied = []  # Untracked files the agent needs; kept out of checkpoint commits.
    for name in GUIDANCE + [".venv"]:
        source = repo / name
        if source.exists() and not (worktree / name).exists():
            copied.append(name)
            if name == ".venv":
                (worktree / name).symlink_to(source)  # ponytail: shared virtualenv; a per-task one if runs install packages.
            elif source.is_dir():
                shutil.copytree(source, worktree / name)
            else:
                (worktree / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, worktree / name)

    task = {
        "id": task_id, "project": args.project, "goal": args.goal, "base": base, "branch": f"hearth/{task_id}",
        "worktree": str(worktree), "status": "running", "stop_reason": None, "created": _now(), "finished": None,
        "discarded": None, "runs": [],
    }
    record = {"role": "implement", "provider": provider, "model": args.model, "effort": args.effort, "sandbox": "local",
              "pid": None, "exit_code": None, "session_id": None, "usage": None, "check_exit_code": None,
              "started": _now(), "finished": None}
    task["runs"].append(record)
    run_dir = task_dir / "runs" / f"01-implement-{provider}"
    run_dir.mkdir(parents=True)
    prompt = PROMPT.format(id=task_id, goal=args.goal, check=project["check"])
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    argv = _argv(provider, prompt, project["check"], args.model, args.effort)
    with (run_dir / "events.jsonl").open("w", encoding="utf-8") as events, (run_dir / "stderr.txt").open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(argv, cwd=worktree, stdin=subprocess.PIPE, stdout=events, stderr=errors, text=True, start_new_session=True)
        record["pid"] = process.pid
        _write(task_dir, task)
        timed_out = False
        try:
            process.communicate("" if "{prompt}" in PROVIDERS[provider] else prompt, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop(process)
    record.update(exit_code=process.returncode, finished=_now())

    parsed = parse_events(provider, (run_dir / "events.jsonl").read_text(encoding="utf-8"))
    record.update(session_id=parsed["session_id"], usage=parsed["usage"])
    (run_dir / "report.md").write_text(parsed["final"], encoding="utf-8")
    if (worktree / ".hearth/artifacts").is_dir():
        shutil.copytree(worktree / ".hearth/artifacts", run_dir / "artifacts", dirs_exist_ok=True)
    _git(worktree, "add", "-A")
    _git(worktree, "reset", "-q", "--", ".hearth", *copied)
    changed = subprocess.run(["git", "-C", str(worktree), "diff", "--cached", "--quiet"]).returncode != 0
    if changed:
        _git(worktree, "commit", "-q", "-m", f"hearth: run 01 implement {provider}")

    stop_reason = "timeout" if timed_out else parsed["error"] or ("provider_error" if process.returncode else None)
    stop_reason = stop_reason or (None if changed else "no_changes")
    if stop_reason is None:
        check = subprocess.run(project["check"], shell=True, cwd=worktree, capture_output=True, text=True)
        (run_dir / "checks.txt").write_text(f"$ {project['check']}\n{check.stdout}{check.stderr}\nexit code: {check.returncode}\n", encoding="utf-8")
        record["check_exit_code"] = check.returncode
        stop_reason = "check_failed" if check.returncode else None
    (task_dir / "diff.patch").write_text(_git(worktree, "diff", f"{base}..HEAD"), encoding="utf-8")
    task.update(status="failed" if stop_reason else "done", stop_reason=stop_reason, finished=_now())
    _write(task_dir, task)
    print(f"{task_id}  {task['status']}{f' ({stop_reason})' if stop_reason else ''}  {worktree}")
    print(f"Next: hearth task show {task_id}", file=sys.stderr)
    return 1 if stop_reason else 0


def _argv(provider: str, prompt: str, check: str, model: str | None, effort: str | None) -> list[str]:
    options = []
    if model:
        options += ["-m" if provider == "codex" else "--model", model]
    if effort:
        options += ["-c", f"model_reasoning_effort={effort}"] if provider == "codex" else ["--effort", effort]
    argv = []
    for part in PROVIDERS[provider]:
        argv += options if part == "{options}" else [prompt if part == "{prompt}" else part.replace("{check}", check)]
    return argv


def _stop(process: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
            process.wait(timeout=5)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            continue


def _show(task: dict, task_dir: Path, as_json: bool) -> None:
    if as_json:
        print(json.dumps(task))
        return
    print(f"{task['id']}  {task['status']}{f' ({task['stop_reason']})' if task['stop_reason'] else ''}")
    print(f"goal      {task['goal']}")
    print(f"branch    {task['branch']}")
    print(f"worktree  {task['worktree']}")
    print(f"receipts  {task_dir}")
    for run in task["runs"]:
        print(f"run       {run['role']} {run['provider']} {run['model'] or ''} exit {run['exit_code']}, check exit {run['check_exit_code']}")
    repo = _repo(task)
    print("To review and publish:")
    print(f"  code {task['worktree']}")
    print(f"  git -C {repo} push -u origin {task['branch']}")
    print(f"  (cd {repo} && gh pr create --head {task['branch']} --fill)")


def _discard(task: dict, task_dir: Path, apply: bool) -> int:
    repo = _repo(task)
    if not apply:
        print(f"Would remove worktree {task['worktree']} and branch {task['branch']}; {task_dir} stays as a receipt.")
        print(f"Next: hearth task discard {task['id']} --apply", file=sys.stderr)
        return 0
    if Path(task["worktree"]).exists():
        _git(repo, "worktree", "remove", "--force", task["worktree"])
    if _git(repo, "branch", "--list", task["branch"]).strip():
        _git(repo, "branch", "-D", task["branch"])
    task["discarded"] = _now()
    _write(task_dir, task)
    print(f"Removed worktree and branch for {task['id']}.")
    return 0


def _repo(task: dict) -> Path:
    projects = json.loads((_home() / "projects.json").read_text(encoding="utf-8"))
    return Path(projects[task["project"]]["path"]).expanduser()


def _read(task_dir: Path) -> dict:
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    pid = task["runs"][-1]["pid"] if task["runs"] else None
    if task["status"] == "running" and not _alive(pid):
        task["status"] = "interrupted"  # Displayed only; the record is not rewritten.
    return task


def _alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def _write(task_dir: Path, task: dict) -> None:
    temporary = task_dir / "task.json.tmp"
    temporary.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    temporary.replace(task_dir / "task.json")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _home() -> Path:
    return Path.home() / ".hearth"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
