from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearth.cli import main
from hearth.workbench import usage

NOW = 1_790_000_000
DAY = 86_400


def _codex_line(rate_limits: dict | None) -> str:
    return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": rate_limits}})


def _codex_limits(five_hour: float, week: float) -> dict:
    return {
        "primary": {"used_percent": five_hour, "window_minutes": 300, "resets_at": NOW + 3600},
        "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": NOW + 4 * DAY},
        "plan_type": "plus",
    }


def _claude_status(five_hour: float, week: float) -> dict:
    return {
        "model": {"display_name": "Opus"},
        "rate_limits": {
            "five_hour": {"used_percentage": five_hour, "resets_at": NOW + 3600},
            "seven_day": {"used_percentage": week, "resets_at": NOW + 2 * DAY},
        },
    }


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary_directory.name)
        self.sessions = self.home / ".codex/sessions"
        self.samples = self.home / ".hearth/usage/claude-limits.jsonl"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _session(self, name: str, lines: list[str], mtime: int) -> None:
        path = self.sessions / "2026/09/26" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))

    def test_codex_uses_the_newest_session_and_skips_null_rate_limits(self) -> None:
        self._session("older.jsonl", [_codex_line(_codex_limits(90, 90))], NOW - 100)
        self._session(
            "newer.jsonl",
            [_codex_line(_codex_limits(10, 40)), json.dumps({"type": "session_meta"}), _codex_line(None), '{"type":"token_count'],
            NOW,
        )

        limits = usage.codex_limits(self.sessions)

        self.assertEqual(limits["secondary"]["used_percent"], 40)

    def test_codex_falls_back_to_an_older_session_when_the_newest_has_no_sample(self) -> None:
        self._session("older.jsonl", [_codex_line(_codex_limits(5, 15))], NOW - 100)
        self._session("newer.jsonl", [_codex_line(None)], NOW)

        self.assertEqual(usage.codex_limits(self.sessions)["secondary"]["used_percent"], 15)

    def test_missing_logs_report_nothing(self) -> None:
        self.assertIsNone(usage.codex_limits(self.sessions))
        self.assertIsNone(usage.claude_limits(self.samples))

    def test_statusline_appends_only_when_the_limits_change(self) -> None:
        for status in (_claude_status(12, 18), _claude_status(12, 18), _claude_status(13, 18)):
            usage.record_statusline(status, self.samples, now=NOW)

        lines = self.samples.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(usage.claude_limits(self.samples)["five_hour"]["used_percentage"], 13)

    def test_statusline_without_rate_limits_records_nothing(self) -> None:
        usage.record_statusline({"model": {"display_name": "Opus"}}, self.samples, now=NOW)

        self.assertFalse(self.samples.exists())

    def test_report_spreads_the_remaining_weekly_limit_over_the_days_left(self) -> None:
        rows = usage.report(_codex_limits(24, 20), _claude_status(12, 50)["rate_limits"], now=NOW)

        by_provider = {row["provider"]: row for row in rows}
        self.assertEqual(by_provider["codex"]["remaining_per_day"], 20.0)
        self.assertEqual(by_provider["claude"]["remaining_per_day"], 25.0)
        self.assertEqual(by_provider["codex"]["five_hour"], 24)
        self.assertIsNone(by_provider["antigravity"]["week"])

    def test_report_blanks_a_window_that_has_reset_since_the_sample(self) -> None:
        limits = _claude_status(12, 50)["rate_limits"]

        row = usage.report(None, limits, now=NOW + 3 * DAY)[0]

        self.assertIsNone(row["five_hour"])
        self.assertIsNone(row["week"])
        self.assertIsNone(row["remaining_per_day"])

    def test_cli_prints_one_json_value_for_all_three_providers(self) -> None:
        self._session("newer.jsonl", [_codex_line(_codex_limits(10, 40))], NOW)
        output = io.StringIO()

        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), contextlib.redirect_stdout(output):
            status = main(["usage", "--json"])

        self.assertEqual(status, 0)
        self.assertEqual([row["provider"] for row in json.loads(output.getvalue())], ["claude", "codex", "antigravity"])

    def test_cli_table_keeps_a_gap_after_every_provider_name(self) -> None:
        output = io.StringIO()

        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), contextlib.redirect_stdout(output):
            main(["usage"])

        for line, provider in zip(output.getvalue().splitlines()[1:], ["claude", "codex", "antigravity"]):
            self.assertTrue(line.startswith(provider + " "), line)

    def test_cli_statusline_records_stdin_and_prints_one_line(self) -> None:
        output = io.StringIO()

        with (
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch("sys.stdin", io.StringIO(json.dumps(_claude_status(12, 18)))),
            contextlib.redirect_stdout(output),
        ):
            status = main(["usage", "statusline"])

        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertIn("12%", output.getvalue())
        self.assertTrue(self.samples.exists())


class WorkbenchBoundaryTests(unittest.TestCase):
    def test_only_the_cli_imports_the_workbench(self) -> None:
        package = Path(__file__).resolve().parents[1] / "src/hearth"
        offenders = []
        for path in package.rglob("*.py"):
            relative = path.relative_to(package)
            if relative.parts[0] == "workbench" or relative == Path("cli.py"):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    prefix = "." * node.level + (node.module or "")
                    names = [prefix] + [f"{prefix}.{alias.name}".replace("..", ".") for alias in node.names]
                if any("workbench" in name.split(".") for name in names):
                    offenders.append(str(relative))

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
