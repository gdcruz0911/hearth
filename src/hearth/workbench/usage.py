"""Plan limits for the three providers, read from logs the provider tools already write."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

DAY = 86_400


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    parser = subcommands.add_parser("usage", help="Show five-hour and weekly plan use for Claude, Codex, and Antigravity.")
    parser.add_argument("--json", action="store_true", help="Print one JSON list instead of a table.")
    actions = parser.add_subparsers(dest="usage_command")
    actions.add_parser("statusline", help="Record Claude Code status line JSON from standard input and print one line.")


def run(args: argparse.Namespace) -> int:
    home = Path.home()
    samples = home / ".hearth/usage/claude-limits.jsonl"
    if args.usage_command == "statusline":
        status = json.load(sys.stdin)
        record_statusline(status, samples)
        print(_statusline(status.get("rate_limits") or {}))
        return 0
    rows = report(codex_limits(home / ".codex/sessions"), claude_limits(samples))
    if args.json:
        print(json.dumps(rows))
    else:
        _print_table(rows)
    return 0


def codex_limits(sessions: Path) -> dict | None:
    """Return `rate_limits` from the newest Codex `token_count` event that has them."""
    for path in sorted(sessions.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            if '"token_count"' not in line:
                continue
            try:
                payload = json.loads(line).get("payload") or {}
            except json.JSONDecodeError:  # Codex may be mid-write on its newest line.
                continue
            if payload.get("type") == "token_count" and payload.get("rate_limits"):
                return payload["rate_limits"]
    return None


def claude_limits(samples: Path) -> dict | None:
    line = _last_line(samples)
    return json.loads(line)["rate_limits"] if line else None


def record_statusline(status: dict, samples: Path, now: float | None = None) -> None:
    limits = status.get("rate_limits")
    if not limits or limits == claude_limits(samples):
        return
    samples.parent.mkdir(parents=True, exist_ok=True)
    with samples.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"at": int(now if now is not None else time.time()), "rate_limits": limits}) + "\n")


def report(codex: dict | None, claude: dict | None, now: float | None = None) -> list[dict]:
    now = now if now is not None else time.time()
    claude = claude or {}
    codex = codex or {}
    return [
        _row("claude", "pro", claude.get("five_hour"), claude.get("seven_day"), "used_percentage", now),
        _row("codex", codex.get("plan_type") or "plus", codex.get("primary"), codex.get("secondary"), "used_percent", now),
        # agy writes no plan limits Hearth can read yet.
        _row("antigravity", "ai pro", None, None, "", now),
    ]


def _row(provider: str, plan: str, five_hour: dict | None, week: dict | None, key: str, now: float) -> dict:
    five_hour = five_hour if five_hour and five_hour["resets_at"] > now else None
    week = week if week and week["resets_at"] > now else None
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
    }


def _print_table(rows: list[dict]) -> None:
    print(f"{'provider':<13}{'plan':<8}{'5-hour':>7}{'week':>7}  {'week resets':<18}{'left per day':>12}")
    for row in rows:
        resets = datetime.fromtimestamp(row["week_resets_at"]).strftime("%a %b %d %H:%M") if row["week_resets_at"] else "-"
        print(
            f"{row['provider']:<13}{row['plan']:<8}{_percent(row['five_hour']):>7}{_percent(row['week']):>7}"
            f"  {resets:<18}{_percent(row['remaining_per_day']):>12}"
        )
    missing = [row["provider"] for row in rows if row["week"] is None and row["provider"] != "antigravity"]
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
