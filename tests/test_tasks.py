from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearth.cli import main
from hearth.workbench import tasks

FAKE_AGENT = [sys.executable, str(Path(__file__).with_name("fake_agent.py")), "{options}", "--allowedTools", "Bash({check} *)"]
FIXTURES = Path(__file__).parent / "fixtures/public/providers"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class TaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary_directory.name)
        self.repo = self.home / "project"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "person@example.com")
        git(self.repo, "config", "user.name", "Person")
        (self.repo / ".gitignore").write_text("AGENTS.md\n.venv/\n", encoding="utf-8")
        (self.repo / "README.md").write_text("# Project\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        (self.repo / "AGENTS.md").write_text("Run the check.\n", encoding="utf-8")
        self.write_projects(check="test -f hello.txt")
        patches = [
            mock.patch.dict(os.environ, {"HOME": str(self.home), "FAKE_AGENT_SCENARIO": "edit"}),
            mock.patch.dict(tasks.PROVIDERS, {"claude": FAKE_AGENT}),
            mock.patch.object(tasks.usage, "report", return_value=[]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_projects(self, check: str) -> None:
        config = self.home / ".hearth/projects.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"demo": {"path": str(self.repo), "check": check, "providers": ["claude"]}}), encoding="utf-8")

    def cli(self, *argv: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = main(list(argv))
        return status, output.getvalue()

    def only_task(self) -> dict:
        (task_dir,) = (self.home / ".hearth/tasks").iterdir()
        return json.loads((task_dir / "task.json").read_text(encoding="utf-8"))

    def test_a_successful_run_leaves_a_checkpoint_and_a_complete_task_directory(self) -> None:
        status, _ = self.cli("task", "new", "demo", "Add hello.txt", "--model", "fake-model")

        task = self.only_task()
        task_dir = self.home / ".hearth/tasks" / task["id"]
        run_dir = task_dir / "runs/01-implement-claude"
        worktree = Path(task["worktree"])
        self.assertEqual(status, 0)
        self.assertEqual(task["status"], "done")
        self.assertIn("Add hello.txt", (run_dir / "prompt.md").read_text(encoding="utf-8"))
        self.assertIn("`test -f hello.txt`", (run_dir / "prompt.md").read_text(encoding="utf-8"))
        self.assertEqual((run_dir / "report.md").read_text(encoding="utf-8"), "Added hello.txt.")
        self.assertIn("exit code: 0", (run_dir / "checks.txt").read_text(encoding="utf-8"))
        self.assertEqual((run_dir / "artifacts/note.txt").read_text(encoding="utf-8"), "for the person\n")
        self.assertIn(f"+# Task {task['id']}", (task_dir / "diff.patch").read_text(encoding="utf-8"))
        self.assertEqual(task["runs"][0]["session_id"], "00000000-0000-0000-0000-00000000000f")
        argv = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])["argv"]
        self.assertIn("fake-model", argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Bash(test -f hello.txt *)")
        self.assertEqual(git(worktree, "log", "-1", "--format=%s"), "hearth: run 01 implement claude\n")
        self.assertEqual(git(worktree, "show", "--name-only", "--format=", "HEAD"), "hello.txt\n")
        self.assertEqual((worktree / "AGENTS.md").read_text(encoding="utf-8"), "Run the check.\n")

    def test_a_failed_check_fails_the_task(self) -> None:
        self.write_projects(check="exit 3")

        status, _ = self.cli("task", "new", "demo", "Add hello.txt")

        task = self.only_task()
        self.assertEqual(status, 1)
        self.assertEqual((task["status"], task["stop_reason"]), ("failed", "check_failed"))

    def test_a_provider_failure_stops_before_the_check(self) -> None:
        os.environ["FAKE_AGENT_SCENARIO"] = "fail"

        status, _ = self.cli("task", "new", "demo", "Add hello.txt")

        task = self.only_task()
        self.assertEqual(status, 1)
        self.assertEqual((task["status"], task["stop_reason"]), ("failed", "provider_error"))
        self.assertIsNone(task["runs"][0]["check_exit_code"])

    def test_a_run_that_changes_nothing_fails_even_when_the_provider_reports_success(self) -> None:
        os.environ["FAKE_AGENT_SCENARIO"] = "idle"

        status, _ = self.cli("task", "new", "demo", "Add hello.txt")

        task = self.only_task()
        self.assertEqual(status, 1)
        self.assertEqual((task["status"], task["stop_reason"]), ("failed", "no_changes"))

    def test_an_expired_sign_in_is_named(self) -> None:
        os.environ["FAKE_AGENT_SCENARIO"] = "auth"

        self.cli("task", "new", "demo", "Add hello.txt")

        self.assertEqual(self.only_task()["stop_reason"], "auth_expired")

    def test_a_run_past_its_timeout_is_stopped(self) -> None:
        os.environ["FAKE_AGENT_SCENARIO"] = "hang"

        status, _ = self.cli("task", "new", "demo", "Add hello.txt", "--timeout", "1")

        task = self.only_task()
        self.assertEqual(status, 1)
        self.assertEqual(task["stop_reason"], "timeout")
        with self.assertRaises(ProcessLookupError):
            os.kill(task["runs"][0]["pid"], 0)

    def test_a_running_task_whose_process_is_gone_lists_as_interrupted(self) -> None:
        self.cli("task", "new", "demo", "Add hello.txt")
        task = self.only_task()
        task.update(status="running", finished=None)
        task["runs"][0]["pid"] = 999_999_999
        (self.home / ".hearth/tasks" / task["id"] / "task.json").write_text(json.dumps(task), encoding="utf-8")

        _, output = self.cli("task", "list", "--json")

        self.assertEqual(json.loads(output)[0]["status"], "interrupted")

    def test_the_headroom_gate_refuses_a_nearly_spent_provider_unless_forced(self) -> None:
        spent = [{"provider": "claude", "five_hour": 95, "week": 40}]
        with mock.patch.object(tasks.usage, "report", return_value=spent):
            refused, _ = self.cli("task", "new", "demo", "Add hello.txt")
            self.assertFalse((self.home / ".hearth/tasks").exists())
            forced, _ = self.cli("task", "new", "demo", "Add hello.txt", "--force")

        self.assertEqual((refused, forced), (1, 0))

    def test_discard_previews_then_removes_the_worktree_and_branch(self) -> None:
        self.cli("task", "new", "demo", "Add hello.txt")
        task = self.only_task()

        self.cli("task", "discard", task["id"])
        self.assertTrue(Path(task["worktree"]).exists())
        self.cli("task", "discard", task["id"], "--apply")

        self.assertFalse(Path(task["worktree"]).exists())
        self.assertNotIn(task["branch"], git(self.repo, "branch", "--list"))
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)
        self.assertIsNotNone(self.only_task()["discarded"])

    def test_show_prints_the_commands_to_review_and_publish(self) -> None:
        self.cli("task", "new", "demo", "Add hello.txt")
        task = self.only_task()

        _, output = self.cli("task", "show", task["id"])

        self.assertIn(f"git -C {self.repo} push -u origin {task['branch']}", output)


class ProviderResultTests(unittest.TestCase):
    def test_each_recorded_provider_stream_yields_final_text_and_session(self) -> None:
        cases = {
            "claude": ("claude-print-2.1.283.jsonl", "`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`"),
            "codex": ("codex-exec-0.157.1.jsonl", "main"),
            "antigravity": ("agy-print-1.2.11.jsonl", "PYTHONPATH=src .venv/bin/python -m unittest discover -s tests\n"),
        }
        for provider, (fixture, final) in cases.items():
            with self.subTest(provider=provider):
                result = tasks.parse_events(provider, (FIXTURES / fixture).read_text(encoding="utf-8"))
                self.assertEqual(result["final"], final)
                self.assertTrue(result["session_id"].startswith("00000000"))
                self.assertIsNone(result["error"])

    def test_the_recorded_expired_sign_in_is_recognized(self) -> None:
        events = (FIXTURES / "claude-print-2.1.220-auth-expired.json").read_text(encoding="utf-8")

        self.assertEqual(tasks.parse_events("claude", events)["error"], "auth_expired")


if __name__ == "__main__":
    unittest.main()
