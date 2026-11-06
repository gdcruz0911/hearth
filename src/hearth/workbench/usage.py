"""Plan limits for Claude and Codex, read from logs the provider tools already write."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from .. import maintenance
from ..runtime import hearth_home

DAY = 86_400


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    parser = subcommands.add_parser("usage", help="Show five-hour and weekly plan use for Claude and Codex.")
    parser.add_argument("--json", action="store_true", help="Print one JSON list instead of a table.")
    actions = parser.add_subparsers(dest="usage_command")
    actions.add_parser("statusline", help="Record Claude Code status line JSON from standard input and print one line.")


def run(args: argparse.Namespace) -> int:
    samples = hearth_home() / "usage/claude-limits.jsonl"
    if args.usage_command == "statusline":
        status = json.load(sys.stdin)
        with contextlib.suppress(maintenance.Held), maintenance.writing(hearth_home()):
            record_statusline(status, samples)  # Skipped during an update; the statusline itself must never fail.
        print(_statusline(status.get("rate_limits") or {}))
        return 0
    codex, codex_at = _codex_sample(*codex_sessions())
    claude, claude_at = _claude_sample(samples)
    rows = report(codex, claude, codex_at=codex_at, claude_at=claude_at)
    if args.json:
        print(json.dumps(rows))
    else:
        _print_table(rows)
    return 0


def codex_sessions() -> list[Path]:
    """The person's own Codex sessions, and those of Hearth's Codex workers, which each task keeps in its own CODEX_HOME."""
    return [Path.home() / ".codex/sessions", *sorted(hearth_home().glob("tasks/*/codex-home/sessions"))]


def codex_limits(*sessions: Path) -> dict | None:
    """Return `rate_limits` from the newest Codex `token_count` event that has them, across the given session folders."""
    return _codex_sample(*sessions)[0]


def _codex_sample(*sessions: Path) -> tuple[dict | None, int | None]:
    for path in sorted((path for folder in sessions for path in folder.rglob("*.jsonl")), key=lambda p: p.stat().st_mtime, reverse=True):
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            if '"token_count"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:  # Codex may be mid-write on its newest line.
                continue
            payload = event.get("payload") or {}
            if payload.get("type") == "token_count" and payload.get("rate_limits"):
                return payload["rate_limits"], _epoch(event.get("timestamp"))
    return None, None


def claude_limits(samples: Path) -> dict | None:
    return _claude_sample(samples)[0]


def _claude_sample(samples: Path) -> tuple[dict | None, int | None]:
    line = _last_line(samples)
    if not line:
        return None, None
    sample = json.loads(line)
    return sample["rate_limits"], sample.get("at")


def record_statusline(status: dict, samples: Path, now: float | None = None) -> None:
    limits = status.get("rate_limits")
    if not limits or limits == claude_limits(samples):
        return
    samples.parent.mkdir(parents=True, exist_ok=True)
    with samples.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"at": int(now if now is not None else time.time()), "rate_limits": limits}) + "\n")


def report(codex: dict | None, claude: dict | None, now: float | None = None, codex_at: int | None = None, claude_at: int | None = None) -> list[dict]:
    now = now if now is not None else time.time()
    claude = claude or {}
    codex = codex or {}
    return [
        _row("claude", "pro", claude.get("five_hour"), claude.get("seven_day"), "used_percentage", now, claude_at),
        _row("codex", codex.get("plan_type") or "plus", codex.get("primary"), codex.get("secondary"), "used_percent", now, codex_at),
    ]


def _row(provider: str, plan: str, five_hour: dict | None, week: dict | None, key: str, now: float, sampled_at: int | None) -> dict:
    # A window the sample lacks is left out; one that has reset since the sample stays listed, with its numbers unknown.
    windows = [_window(label, _current(window, now), key, sampled_at) for label, window in (("5-hour", five_hour), ("week", week)) if window]
    five_hour = _current(five_hour, now)
    week = _current(week, now)
    remaining = None
    if week:
        days_left = max((week["resets_at"] - now) / DAY, 1)
        remaining = round((100 - week[key]) / days_left, 1)
    return {
        "provider": provider,
        "plan": plan,
        "five_hour": five_hour[key] if five_hour else None,
        "week": week[key] if week else None,
        "week_resets_at": week["resets_at"] if week else None,
        "remaining_per_day": remaining,
        "windows": windows,
    }


def _current(window: dict | None, now: float) -> dict | None:
    return window if window and window["resets_at"] > now else None


def _window(label: str, current: dict | None, key: str, sampled_at: int | None) -> dict:
    return {
        "label": label,
        "used_percent": current[key] if current else None,
        "resets_at": current["resets_at"] if current else None,
        "sampled_at": sampled_at,
    }


def _epoch(timestamp: str | None) -> int | None:
    try:
        return int(datetime.fromisoformat(timestamp).timestamp())
    except (TypeError, ValueError):
        return None


def _print_table(rows: list[dict]) -> None:
    print(f"{'provider':<13}{'plan':<8}{'5-hour':>7}{'week':>7}  {'week resets':<18}{'left per day':>12}")
    for row in rows:
        resets = datetime.fromtimestamp(row["week_resets_at"]).strftime("%a %b %d %H:%M") if row["week_resets_at"] else "-"
        print(
            f"{row['provider']:<13}{row['plan']:<8}{_percent(row['five_hour']):>7}{_percent(row['week']):>7}"
            f"  {resets:<18}{_percent(row['remaining_per_day']):>12}"
        )
    missing = [row["provider"] for row in rows if row["week"] is None]
    if missing:
        print(f"Next: use {' and '.join(missing)} once to record a current sample.", file=sys.stderr)


def _statusline(limits: dict) -> str:
    five_hour = (limits.get("five_hour") or {}).get("used_percentage")
    week = (limits.get("seven_day") or {}).get("used_percentage")
    return f"5h {_percent(five_hour)} | week {_percent(week)}"


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:g}%"


def _last_line(path: Path) -> str | None:
    try:
        with path.open("rb") as file:
            file.seek(0, 2)
            file.seek(max(file.tell() - 4096, 0))
            lines = file.read().decode("utf-8").splitlines()
    except FileNotFoundError:
        return None
    return lines[-1] if lines else None
