"""What each provider and role has done, read from task records: the data layer for the command center."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import usage

JUDGES = {"review": "approve", "verify": "verified"}  # Roles whose result is a verdict, and the verdict that passes.


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    parser = subcommands.add_parser("stats", help="Show runs, usable replies, pass rates, time, models, and escapes per provider and role.")
    parser.add_argument("--json", action="store_true", help="Print one JSON value for the command center.")


def run(args: argparse.Namespace) -> int:
    tasks = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((Path.home() / ".hearth/tasks").glob("*/task.json"))]
    report = {"rows": rows(tasks), "summary": summary(tasks), "usage": _usage()}
    if args.json:
        print(json.dumps(report))
        return 0
    print(f"{'provider':<13}{'role':<11}{'runs':>5}{'usable':>8}{'passed':>8}{'avg min':>9}  {'effort':<14}models")
    for row in report["rows"]:
        print(f"{row['provider']:<13}{row['role']:<11}{row['runs']:>5}{_percent(row['usable']):>8}{_percent(row['passed']):>8}"
              f"{'-' if row['minutes'] is None else row['minutes']:>9}  {','.join(row['efforts']) or '-':<14}{','.join(row['models']) or '-'}")
    s = report["summary"]
    print(f"tasks {s['tasks']} · done {s['done']} · failed {s['failed']} · waiting {s['waiting']} · "
          f"fix rounds per task {s['fix_rounds']} · escapes {s['escapes']} · escapes per done task {s['escapes_per_done_task']}")
    print("plan use this week: " + ", ".join(f"{row['provider']} {_percent((row['week'] or 0) / 100) if row['week'] is not None else '-'}" for row in report["usage"]))
    print("Escapes are counted per done task, because Hearth cannot see which pull requests were merged.")
    return 0


def rows(tasks: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for task in tasks:
        for record in task.get("runs", []):
            groups[(record["provider"], record["role"])].append(record)
    result = []
    for (provider, role), runs in sorted(groups.items()):
        minutes = [m for m in map(_minutes, runs) if m is not None]
        result.append({
            "provider": provider, "role": role, "runs": len(runs),
            # An interactive run has no exit code; the person drove it, so it counts as usable.
            "usable": _rate(runs, lambda r: r.get("verdict") is not None if role in JUDGES else r.get("exit_code") == 0 or bool(r.get("interactive"))),
            "passed": _rate(runs, lambda r: _passed(role, r)),
            "minutes": round(sum(minutes) / len(minutes), 1) if minutes else None,
            "models": sorted({r["model_used"] for r in runs if r.get("model_used")}),
            "efforts": sorted({r["effort"] for r in runs if r.get("effort")}),
        })
    return result


def summary(tasks: list[dict]) -> dict:
    done = sum(task.get("status") == "done" for task in tasks)
    escapes = sum(len(task.get("escapes", [])) for task in tasks)
    fixes = sum(run["role"] == "fix" for task in tasks for run in task.get("runs", []))
    return {
        "tasks": len(tasks), "done": done,
        "failed": sum(task.get("status") == "failed" for task in tasks),
        "waiting": sum(task.get("status") == "waiting" for task in tasks),
        "fix_rounds": round(fixes / len(tasks), 2) if tasks else 0,
        "escapes": escapes, "escapes_per_done_task": round(escapes / done, 2) if done else 0,
    }


def _passed(role: str, run: dict) -> bool:
    if role in JUDGES:
        return run.get("verdict") == JUDGES[role]
    if role == "test":
        return run.get("check_exit_code") not in (0, None)  # Tests written first must fail before the change.
    if role in ("implement", "fix"):
        return run.get("check_exit_code") == 0 and run.get("guards") != "fail"
    return run.get("exit_code") == 0


def _rate(runs: list[dict], test) -> float:
    return round(sum(map(test, runs)) / len(runs), 2)


def _minutes(run: dict) -> float | None:
    try:
        start, end = (datetime.strptime(run[key], "%Y-%m-%dT%H:%M:%S%z") for key in ("started", "finished"))
    except (KeyError, TypeError, ValueError):
        return None
    return (end - start).total_seconds() / 60


def _usage() -> list[dict]:
    home = Path.home()
    return usage.report(usage.codex_limits(home / ".codex/sessions"), usage.claude_limits(home / ".hearth/usage/claude-limits.jsonl"))


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{round(value * 100)}%"
