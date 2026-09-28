from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hearth.cli import main
from hearth.workbench import tasks

FAKE_AGENT = [sys.executable, str(Path(__file__).with_name("fake_agent.py")), "{options}", "--allowedTools", "Bash({check} *)"]
FAKE_CODEX = [sys.executable, str(Path(__file__).with_name("fake_agent.py")), "--as", "codex", "{options}"]
FAKE_AGY = [sys.executable, str(Path(__file__).with_name("fake_agent.py")), "--as", "antigravity", "{options}"]
FIXTURES = Path(__file__).parent / "fixtures/public/providers"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class TaskTestCase(unittest.TestCase):
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
        os.environ.pop("HEARTH_TASK", None)  # Restored by the patch; set when an agent runs this suite inside a task.

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_projects(self, check: str, providers: tuple[str, ...] = ("claude",)) -> None:
        config = self.home / ".hearth/projects.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"demo": {"path": str(self.repo), "check": check, "providers": list(providers)}}), encoding="utf-8")

    def cli(self, *argv: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = main(list(argv))
        return status, output.getvalue()

    def only_task_by_id(self, task_id: str) -> dict:
        return json.loads((self.home / ".hearth/tasks" / task_id / "task.json").read_text(encoding="utf-8"))

    def only_task(self) -> dict:
        (task_dir,) = (self.home / ".hearth/tasks").iterdir()
        return json.loads((task_dir / "task.json").read_text(encoding="utf-8"))


class TaskTests(TaskTestCase):
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
        self.assertEqual((run_dir / "report.md").read_text(encoding="utf-8"), "Added hello.txt.\nPR title: feat: add hello.txt\nPR summary: hello.txt now greets the person.")
        self.assertIn("exit code: 0", (run_dir / "checks.txt").read_text(encoding="utf-8"))
        self.assertEqual((run_dir / "artifacts/note.txt").read_text(encoding="utf-8"), "for the person\n")
        self.assertIn(f"+# Task {task['id']}", (task_dir / "diff.patch").read_text(encoding="utf-8"))
        self.assertEqual(task["runs"][0]["session_id"], "00000000-0000-0000-0000-00000000000f")
        argv = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])["argv"]
        self.assertIn("fake-model", argv)
        self.assertEqual(json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])["hearth_task"], task["id"])
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Bash(test -f hello.txt *)")
        self.assertEqual(git(worktree, "log", "-1", "--format=%s"), "hearth: run 01 implement claude\n")
        self.assertEqual(git(worktree, "show", "--name-only", "--format=", "HEAD"), "hello.txt\n")
        self.assertEqual((worktree / "AGENTS.md").read_text(encoding="utf-8"), "Run the check.\n")

    def test_an_agent_cannot_start_a_task(self) -> None:
        os.environ["HEARTH_TASK"] = "20260927-000000"

        status, _ = self.cli("task", "new", "demo", "Add hello.txt")

        self.assertEqual(status, 1)
        self.assertFalse((self.home / ".hearth/tasks").exists())

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

    def test_a_task_can_be_named_by_last_or_a_unique_ending_of_its_id(self) -> None:
        self.cli("task", "new", "demo", "Add hello.txt")
        task = self.only_task()

        _, by_last = self.cli("task", "show", "last")
        _, by_ending = self.cli("task", "show", task["id"][-6:])

        self.assertIn(task["id"], by_last)
        self.assertIn(task["id"], by_ending)
        self.assertEqual(self.cli("task", "show", "999999")[0], 1)

    def test_show_prints_the_commands_to_review_and_publish(self) -> None:
        self.cli("task", "new", "demo", "Add hello.txt")
        task = self.only_task()

        _, output = self.cli("task", "show", task["id"])

        self.assertIn(f"git -C {self.repo} push -u origin {task['branch']}", output)
        self.assertIn("gh pr create --head", output)
        self.assertIn("--title 'Add hello.txt'", output)
        self.assertNotIn("--fill", output)

    def test_show_lists_each_run_with_its_model_effort_and_outcome(self) -> None:
        self.cli("task", "new", "demo", "Add hello.txt")
        task = self.only_task()

        _, output = self.cli("task", "show", task["id"])

        self.assertIn("01 implement  claude       fake-model", output)
        self.assertIn("medium", output)
        self.assertIn("check pass, guards pass", output)
        self.assertNotIn("check exit None", output)


class InteractiveTaskTests(TaskTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.launched: list[list[str]] = []
        patches = [
            mock.patch.object(tasks, "_launch", side_effect=lambda argv: self.launched.append(argv)),
            mock.patch.object(tasks, "_has_session", return_value=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("TMUX", None)  # Restored by the HOME patch; attach, not switch-client, outside tmux.

    def test_interactive_opens_the_cli_in_a_tmux_window_then_collect_records_the_work(self) -> None:
        status, _ = self.cli("task", "new", "demo", "Add hello.txt", "--interactive", "--model", "fake-model")

        task = self.only_task()
        worktree = Path(task["worktree"])
        self.assertEqual((status, task["status"]), (0, "waiting"))
        window = self.launched[-2]
        self.assertEqual(self.launched[-1], ["tmux", "attach", "-t", f"=hearth-demo:{task['id']}"])
        self.assertEqual(window[:10], ["tmux", "new-window", "-t", "=hearth-demo:", "-c", str(worktree), "-n", task["id"], "-e", f"HEARTH_TASK={task['id']}"])
        # The prompt precedes the options, because --allowedTools takes several values and would swallow it.
        self.assertEqual(window[10], "claude")
        self.assertIn("Add hello.txt", window[11])
        self.assertEqual(window[12:14], ["--model", "fake-model"])

        (worktree / "hello.txt").write_text("by hand\n", encoding="utf-8")
        collected, _ = self.cli("task", "collect", task["id"])

        task = self.only_task()
        self.assertEqual((collected, task["status"]), (0, "done"))
        self.assertEqual(git(worktree, "show", "--name-only", "--format=", "HEAD"), "hello.txt\n")

    def test_discard_closes_the_task_window(self) -> None:
        self.cli("task", "new", "demo", "Add hello.txt", "--interactive")
        task = self.only_task()

        with mock.patch.object(tasks, "_close_window") as closed:
            self.cli("task", "discard", task["id"], "--apply")

        closed.assert_called_once_with("demo", task["id"])

    def test_collect_without_changes_fails(self) -> None:
        self.cli("task", "new", "demo", "Add hello.txt", "--interactive")

        status, _ = self.cli("task", "collect", self.only_task()["id"])

        self.assertEqual((status, self.only_task()["stop_reason"]), (1, "no_changes"))

    def test_attachments_are_copied_and_listed_in_the_prompt(self) -> None:
        sketch = self.home / "sketch.png"
        sketch.write_bytes(b"png")

        self.cli("task", "new", "demo", "Add hello.txt", "--interactive", "--attach", str(sketch))

        task = self.only_task()
        self.assertEqual((Path(task["worktree"]) / ".hearth/attachments/sketch.png").read_bytes(), b"png")
        prompt = (self.home / ".hearth/tasks" / task["id"] / "runs/01-implement-claude/prompt.md").read_text(encoding="utf-8")
        self.assertIn(".hearth/attachments/sketch.png", prompt)

    def test_an_issue_starts_the_brief(self) -> None:
        issue = {"title": "Add hello.txt", "body": "It should say hello."}
        with mock.patch.object(tasks, "_issue", return_value=issue) as fetched:
            self.cli("task", "new", "demo", "--issue", "7", "--interactive")

        task = self.only_task()
        fetched.assert_called_once_with(self.repo, 7)
        self.assertEqual(task["goal"], "Add hello.txt")
        self.assertIn("It should say hello.", (self.home / ".hearth/tasks" / task["id"] / "brief.md").read_text(encoding="utf-8"))

    def test_open_launches_vs_code_on_the_worktree(self) -> None:
        self.cli("task", "new", "demo", "Add hello.txt", "--interactive")
        task = self.only_task()

        self.cli("task", "open", task["id"])

        self.assertEqual(self.launched[-1], ["code", task["worktree"]])

    def test_hearth_open_creates_the_project_session_once(self) -> None:
        with mock.patch.object(tasks, "_has_session", return_value=False):
            self.cli("open", "demo")
        created = [argv for argv in self.launched if argv[:2] == ["tmux", "new-session"]]
        self.assertEqual(created[0][:6], ["tmux", "new-session", "-d", "-s", "hearth-demo", "-c"])
        self.assertEqual(self.launched[-1][:2], ["tmux", "attach"])

        self.launched.clear()
        with mock.patch.object(tasks, "_has_session", return_value=True):
            self.cli("open", "demo")
        self.assertEqual([argv[:2] for argv in self.launched], [["tmux", "attach"]])


class LoopTestCase(TaskTestCase):
    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.dict(tasks.REVIEWERS, {"codex": FAKE_CODEX, "antigravity": FAKE_AGY})
        patch.start()
        self.addCleanup(patch.stop)

    def start(self, check: str = "test -f hello.txt") -> dict:
        self.write_projects(check=check, providers=("claude", "codex"))
        self.cli("task", "new", "demo", "Add hello.txt")
        return self.only_task()

    def loop(self, task: dict, reviews: str, *argv: str) -> int:
        os.environ["FAKE_REVIEWS"] = reviews
        return self.cli("loop", task["id"], *argv)[0]


class LoopTests(LoopTestCase):
    def test_an_approving_review_from_another_model_family_finishes_the_task(self) -> None:
        status = self.loop(self.start(), "approve")

        task = self.only_task()
        self.assertEqual((status, task["status"], task["stop_reason"]), (0, "done", None))
        self.assertEqual([(run["role"], run["provider"]) for run in task["runs"]], [("implement", "claude"), ("review", "codex")])
        self.assertEqual(task["review"]["verdict"], "approve")
        prompt = (self.home / ".fake-last-review-prompt").read_text(encoding="utf-8")
        self.assertIn(f"+# Task {task['id']}", prompt)
        self.assertIn("where they exist, AGENTS.md and the standards in docs/standards/", prompt)

    def test_requested_changes_get_a_fix_run_and_another_review(self) -> None:
        status = self.loop(self.start(), "changes,approve")

        task = self.only_task()
        self.assertEqual(status, 0)
        self.assertEqual([run["role"] for run in task["runs"]], ["implement", "review", "fix", "review"])
        self.assertEqual(git(Path(task["worktree"]), "log", "-1", "--format=%s"), "hearth: run 03 fix claude\n")
        fix_prompt = (self.home / ".hearth/tasks" / task["id"] / "runs/03-fix-claude/prompt.md").read_text(encoding="utf-8")
        self.assertIn("CLI-3 hello.txt:1 Say hello.", fix_prompt)

    def test_the_loop_stops_when_its_fix_rounds_run_out(self) -> None:
        status = self.loop(self.start(), "changes,changes", "--rounds", "1")

        task = self.only_task()
        self.assertEqual((status, task["status"], task["stop_reason"]), (1, "failed", "rounds_exhausted"))

    def test_an_unreadable_verdict_stops_the_loop(self) -> None:
        self.assertEqual(self.loop(self.start(), "garbage"), 1)
        self.assertEqual(self.only_task()["stop_reason"], "review_unparsed")

    def test_the_loop_can_be_rerun_after_an_unreadable_verdict(self) -> None:
        task = self.start()
        self.loop(task, "garbage")

        os.environ["FAKE_REVIEWS"] = "garbage,approve"
        status, _ = self.cli("loop", task["id"])

        self.assertEqual((status, self.only_task()["status"]), (0, "done"))
        self.assertIn("Do not run commands", (self.home / ".fake-last-review-prompt").read_text(encoding="utf-8"))

    def test_a_failed_check_is_fixed_before_any_review(self) -> None:
        task = self.start(check="grep -q 'fix round' hello.txt")
        self.assertEqual(task["stop_reason"], "check_failed")

        status = self.loop(task, "approve")

        task = self.only_task()
        self.assertEqual((status, [run["role"] for run in task["runs"]]), (0, ["implement", "fix", "review"]))

    def test_antigravity_reviews_first_and_an_empty_review_falls_back_to_the_next_reviewer(self) -> None:
        self.write_projects(check="test -f hello.txt", providers=("claude", "codex", "antigravity"))
        self.cli("task", "new", "demo", "Add hello.txt")

        status = self.loop(self.only_task(), "garbage,approve")

        task = self.only_task()
        self.assertEqual(status, 0)
        self.assertEqual([(run["role"], run["provider"]) for run in task["runs"]],
                         [("implement", "claude"), ("review", "antigravity"), ("review", "codex")])
        self.assertEqual(task["review"]["reviewer"], "codex")

    def test_a_named_reviewer_gets_no_fallback(self) -> None:
        self.write_projects(check="test -f hello.txt", providers=("claude", "codex", "antigravity"))
        self.cli("task", "new", "demo", "Add hello.txt")

        status = self.loop(self.only_task(), "garbage,approve", "--reviewer", "antigravity")

        self.assertEqual((status, self.only_task()["stop_reason"]), (1, "review_unparsed"))

    def test_a_reviewer_from_the_implementer_family_is_refused(self) -> None:
        self.assertEqual(self.loop(self.start(), "approve", "--reviewer", "claude"), 1)
        self.assertEqual(len(self.only_task()["runs"]), 1)

    def test_work_waits_as_queued_while_its_slot_is_full(self) -> None:
        task = self.start()
        statuses = []
        real_sleep = time.sleep

        def sleep(seconds: float) -> None:
            if seconds == 5:  # Hearth's slot poll; subprocess sleeps briefly while it waits for a process.
                statuses.append(self.only_task()["status"])
            else:
                real_sleep(seconds)

        with mock.patch.object(tasks, "_busy", side_effect=[1, 0]), mock.patch.object(tasks.time, "sleep", side_effect=sleep):
            self.loop(task, "approve")

        self.assertEqual(statuses, ["queued"])


class BoardTests(LoopTestCase):
    def board(self, task: dict) -> list[dict]:
        path = self.home / ".hearth/tasks" / task["id"] / "board.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_a_question_for_the_person_waits_until_answered_and_the_answer_reaches_the_next_prompt(self) -> None:
        os.environ["FAKE_OUTBOX"] = '{"to": "person", "kind": "question", "body": "Which greeting?"}'
        task = self.start()
        os.environ.pop("FAKE_OUTBOX")

        self.assertEqual((task["status"], task["stop_reason"]), ("waiting", "question"))
        self.assertEqual(self.board(task)[0]["from"], "01-implement-claude")
        self.assertEqual(self.loop(task, "approve"), 1)

        _, shown = self.cli("task", "show", task["id"])
        self.assertIn("Which greeting?", shown)

        status, _ = self.cli("task", "answer", task["id"], "Say hello to the person.")
        self.assertEqual((status, self.only_task()["status"]), (0, "done"))
        self.assertEqual(self.board(task)[-1], self.board(task)[-1] | {"from": "person", "to": "implement", "kind": "answer"})

        self.assertEqual(self.loop(task, "changes,approve"), 0)
        fix_prompt = (self.home / ".hearth/tasks" / task["id"] / "runs/03-fix-claude/prompt.md").read_text(encoding="utf-8")
        self.assertIn("Which greeting?", fix_prompt)
        self.assertIn("Say hello to the person.", fix_prompt)
        self.assertIn("Say hello to the person.", (self.home / ".fake-last-review-prompt").read_text(encoding="utf-8"))

    def test_an_agent_that_only_asked_continues_after_the_answer(self) -> None:
        os.environ.update(FAKE_AGENT_SCENARIO="idle", FAKE_OUTBOX='{"to": "person", "kind": "question", "body": "Which flag?"}')
        task = self.start()
        os.environ.update(FAKE_AGENT_SCENARIO="edit")
        os.environ.pop("FAKE_OUTBOX")
        self.cli("task", "answer", task["id"], "Use --json.")

        status = self.loop(task, "approve")

        task = self.only_task()
        self.assertEqual((status, [run["role"] for run in task["runs"]]), (0, ["implement", "fix", "review"]))
        fix_prompt = (self.home / ".hearth/tasks" / task["id"] / "runs/02-fix-claude/prompt.md").read_text(encoding="utf-8")
        self.assertIn("Use --json.", fix_prompt)

    def test_a_fix_that_changes_nothing_on_finished_work_goes_back_to_review(self) -> None:
        task = self.start()
        os.environ["FAKE_AGENT_SCENARIO"] = "idle"

        status = self.loop(task, "changes,approve")

        task = self.only_task()
        self.assertEqual((status, [run["role"] for run in task["runs"]]), (0, ["implement", "review", "fix", "review"]))

    def test_a_handoff_to_the_reviewer_appears_in_its_prompt(self) -> None:
        os.environ["FAKE_OUTBOX"] = '{"to": "review", "kind": "handoff", "body": "Check the empty-file case."}'
        task = self.start()
        os.environ.pop("FAKE_OUTBOX")

        self.loop(task, "approve")

        self.assertIn("Check the empty-file case.", (self.home / ".fake-last-review-prompt").read_text(encoding="utf-8"))

    def test_malformed_or_misaddressed_messages_are_dropped_and_counted(self) -> None:
        os.environ["FAKE_OUTBOX"] = '\n'.join([
            "not json",
            '{"to": "everyone", "kind": "question", "body": "Hi?"}',
            '{"to": "review", "kind": "finding", "body": "Fine."}',
        ])
        task = self.start()
        os.environ.pop("FAKE_OUTBOX")

        self.assertEqual([message["body"] for message in self.board(task)], ["Fine."])
        self.assertEqual(task["runs"][0]["rejected_messages"], 2)
        self.assertFalse((Path(task["worktree"]) / ".hearth/outbox.jsonl").exists())
        receipt = self.home / ".hearth/tasks" / task["id"] / "runs/01-implement-claude/outbox.jsonl"
        self.assertIn("not json", receipt.read_text(encoding="utf-8"))

    def test_answering_a_task_with_no_open_question_is_refused(self) -> None:
        task = self.start()

        status, _ = self.cli("task", "answer", task["id"], "Hello?")

        self.assertEqual(status, 1)


class ReviewEvaluationTests(LoopTestCase):
    CASES = Path(__file__).parent / "fixtures/public/reviews"

    def test_every_case_passes_its_own_tests_so_only_the_reviewer_can_catch_a_planted_bug(self) -> None:
        for case in sorted(path.parent for path in self.CASES.glob("*/case.json")):
            with self.subTest(case=case.name), tempfile.TemporaryDirectory() as work:
                for tree in (self.CASES / "base", case / "after"):
                    for source in tree.glob("*.py"):
                        (Path(work) / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                result = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=work, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_case_is_scored_and_appended_to_the_evaluation_log(self) -> None:
        # Cases run in name order: bug-fraction, bug-negative, bug-skip, bug-swallow, clean-days, clean-rename.
        os.environ["FAKE_REVIEWS"] = "changes,changes,changes,approve,approve,garbage"

        status, output = self.cli("review-eval", str(self.CASES), "--reviewer", "codex")

        rows = [json.loads(line) for line in (self.home / ".hearth/evals/reviews.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(status, 0)
        self.assertEqual([(row["case"], row["verdict"], row["correct"]) for row in rows], [
            ("bug-fraction", "changes", True), ("bug-negative", "changes", True), ("bug-skip", "changes", True), ("bug-swallow", "approve", False),
            ("clean-days", "approve", True), ("clean-rename", None, False),
        ])
        self.assertEqual({row["reviewer"] for row in rows}, {"codex"})
        self.assertIn("planted bugs caught: 3 of 4", output)
        self.assertIn("clean changes approved: 1 of 2", output)
        prompt = (self.home / ".fake-last-review-prompt").read_text(encoding="utf-8")
        self.assertIn("SECONDS_PER_UNIT", prompt)


class VerifyTestCase(LoopTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / "VERIFY.md").write_text("Run the program and save its output as evidence.\n", encoding="utf-8")
        git(self.repo, "add", "VERIFY.md")
        git(self.repo, "commit", "-q", "-m", "add VERIFY.md")
        patch = mock.patch.dict(tasks.VERIFIERS, {"codex": FAKE_CODEX})
        patch.start()
        self.addCleanup(patch.stop)

    def run_loop(self, verify: str, reviews: str = "approve", *argv: str) -> tuple[int, dict]:
        os.environ["FAKE_VERIFY"] = verify
        status = self.loop(self.start(), reviews, *argv)
        return status, self.only_task()


class VerifyTests(VerifyTestCase):
    def test_verified_evidence_is_kept_and_summarized_for_the_reviewer(self) -> None:
        status, task = self.run_loop("verified")

        self.assertEqual((status, [run["role"] for run in task["runs"]]), (0, ["implement", "verify", "review"]))
        run_dir = self.home / ".hearth/tasks" / task["id"] / "runs/02-verify-codex"
        self.assertTrue((run_dir / "evidence/hello.txt").read_text(encoding="utf-8"))
        self.assertIn("pass: hello.txt greets the person (evidence/hello.txt)", (self.home / ".fake-last-review-prompt").read_text(encoding="utf-8"))
        verify_prompt = (self.home / ".fake-last-verify-prompt").read_text(encoding="utf-8")
        self.assertIn("VERIFY.md", verify_prompt)
        self.assertIn("Hearth already ran the project's full check", verify_prompt)
        review_prompt = (self.home / ".fake-last-review-prompt").read_text(encoding="utf-8")
        self.assertIn("not checked: the web interface: no loopback port here", review_prompt)
        self.assertIn("weakens or skips a test", review_prompt)

    def test_a_failed_claim_is_sent_back_as_a_fix_run(self) -> None:
        status, task = self.run_loop("failed,verified")

        self.assertEqual([run["role"] for run in task["runs"]], ["implement", "verify", "fix", "verify", "review"])
        fix_prompt = (self.home / ".hearth/tasks" / task["id"] / "runs/03-fix-claude/prompt.md").read_text(encoding="utf-8")
        self.assertIn("fail: hello.txt greets the person", fix_prompt)

    def test_a_verified_verdict_citing_missing_evidence_is_rejected(self) -> None:
        status, task = self.run_loop("missing")

        self.assertEqual((status, task["stop_reason"]), (1, "verify_rejected"))
        self.assertIn("evidence/hello.txt is missing or empty", (self.home / ".hearth/tasks" / task["id"] / "runs/02-verify-codex/verify.txt").read_text(encoding="utf-8"))

    def test_a_verifier_that_edits_source_stops_the_task_for_the_person(self) -> None:
        status, task = self.run_loop("edit")

        self.assertEqual((status, task["stop_reason"]), (1, "verifier_edited"))
        self.assertEqual((Path(task["worktree"]) / "hello.txt").read_text(encoding="utf-8"), "changed by the verifier\n")

    def test_a_task_recorded_before_copied_files_were_tracked_still_verifies(self) -> None:
        os.environ["FAKE_VERIFY"] = "verified"
        task = self.start()
        del task["copied"]
        (self.home / ".hearth/tasks" / task["id"] / "task.json").write_text(json.dumps(task), encoding="utf-8")
        (Path(task["worktree"]) / ".venv").write_text("an untracked link in an old worktree\n", encoding="utf-8")

        status = self.loop(task, "approve")

        self.assertEqual((status, self.only_task()["status"]), (0, "done"))

    def test_projects_without_verify_md_skip_verification(self) -> None:
        git(self.repo, "rm", "-q", "VERIFY.md")
        git(self.repo, "commit", "-q", "-m", "drop VERIFY.md")

        status, task = self.run_loop("verified")

        self.assertEqual([run["role"] for run in task["runs"]], ["implement", "review"])


class GuardTests(LoopTestCase):
    def test_a_secret_shaped_string_fails_the_guards_without_echoing_the_secret(self) -> None:
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        os.environ["FAKE_EXTRA"] = f"aws_key = {secret}\n"

        task = self.start()

        guards = (self.home / ".hearth/tasks" / task["id"] / "runs/01-implement-claude/guards.txt").read_text(encoding="utf-8")
        self.assertEqual((task["status"], task["stop_reason"]), ("failed", "guard_failed"))
        self.assertIn("hello.txt:2", guards)
        self.assertIn("AWS access key", guards)
        self.assertNotIn(secret, guards)

    def test_an_absolute_home_path_fails_the_guards(self) -> None:
        os.environ["FAKE_EXTRA"] = "see /Users/someone/Documents/notes.txt\n"

        self.assertEqual(self.start()["stop_reason"], "guard_failed")

    def test_a_guard_failure_is_sent_back_as_a_fix_run(self) -> None:
        os.environ["FAKE_EXTRA"] = "see /Users/someone/notes.txt\n"
        task = self.start()
        os.environ.pop("FAKE_EXTRA")

        self.loop(task, "approve", "--rounds", "1")

        fix_prompt = (self.home / ".hearth/tasks" / task["id"] / "runs/02-fix-claude/prompt.md").read_text(encoding="utf-8")
        self.assertIn("absolute home path", fix_prompt)

    def test_a_new_test_skip_fails_the_guards(self) -> None:
        os.environ["FAKE_EXTRA"] = "        self.skipTest('flaky here')\n"

        task = self.start()

        guards = (self.home / ".hearth/tasks" / task["id"] / "runs/01-implement-claude/guards.txt").read_text(encoding="utf-8")
        self.assertEqual(task["stop_reason"], "guard_failed")
        self.assertIn("test skip in hello.txt:2 (TEST-7)", guards)

    def test_raising_unittest_skiptest_fails_the_guards(self) -> None:
        os.environ["FAKE_EXTRA"] = "        raise unittest.SkipTest('flaky here')\n"

        task = self.start()

        guards = (self.home / ".hearth/tasks" / task["id"] / "runs/01-implement-claude/guards.txt").read_text(encoding="utf-8")
        self.assertEqual(task["stop_reason"], "guard_failed")
        self.assertIn("test skip in hello.txt:2 (TEST-7)", guards)

    def test_a_skip_the_goal_asks_for_passes_the_guards(self) -> None:
        os.environ["FAKE_EXTRA"] = "@unittest.skipIf(sys.platform == 'win32', 'POSIX only')\n"
        self.write_projects(check="test -f hello.txt", providers=("claude", "codex"))

        self.cli("task", "new", "demo", "Add hello.txt and skip its test on Windows")

        self.assertEqual(self.only_task()["status"], "done")

    def test_a_diff_over_the_size_limit_fails_the_guards(self) -> None:
        os.environ["FAKE_EXTRA"] = "line\n" * (tasks.MAX_DIFF_LINES + 1)

        self.assertEqual(self.start()["stop_reason"], "guard_failed")

    def test_a_review_objecting_to_protected_tests_asks_the_person_instead_of_the_implementer(self) -> None:
        task = self.start()
        task.update(protected_tests=["hello.txt"], protected_commit=_head(Path(task["worktree"])))
        (self.home / ".hearth/tasks" / task["id"] / "task.json").write_text(json.dumps(task), encoding="utf-8")

        self.assertEqual(self.loop(task, "changes,approve"), 1)
        task = self.only_task()
        self.assertEqual((task["status"], [run["role"] for run in task["runs"]]), ("waiting", ["implement", "review"]))
        review_prompt = (self.home / ".fake-last-review-prompt").read_text(encoding="utf-8")
        self.assertIn("Protected tests, which the implementer cannot change: hello.txt", review_prompt)
        self.assertIn("first on purpose as part of this change, so adding them is in scope", review_prompt)

        self.cli("task", "answer", task["id"], "Keep the tests as they are.")
        self.assertEqual(self.cli("loop", task["id"])[0], 0)
        self.assertIn("Keep the tests as they are.", (self.home / ".fake-last-review-prompt").read_text(encoding="utf-8"))

    def test_a_finding_on_a_line_added_after_protection_goes_to_the_implementer(self) -> None:
        task = self.start()
        task.update(protected_tests=["hello.txt"], protected_commit=_head(Path(task["worktree"])))
        (self.home / ".hearth/tasks" / task["id"] / "task.json").write_text(json.dumps(task), encoding="utf-8")

        status = self.loop(task, "elsewhere,second-line,approve")

        task = self.only_task()
        self.assertEqual((status, [run["role"] for run in task["runs"]]),
                         (0, ["implement", "review", "fix", "review", "fix", "review"]))

    def test_adding_to_a_protected_test_file_passes_the_guards(self) -> None:
        task = self.start()
        task.update(protected_tests=["hello.txt"], protected_commit=_head(Path(task["worktree"])))
        (self.home / ".hearth/tasks" / task["id"] / "task.json").write_text(json.dumps(task), encoding="utf-8")

        self.assertEqual(self.loop(task, "elsewhere,approve"), 0)
        self.assertEqual(self.only_task()["runs"][2]["guards"], "pass")

    def test_a_fix_that_changes_nothing_reruns_the_check_and_guards_on_the_branch(self) -> None:
        os.environ["FAKE_EXTRA"] = "see /Users/someone/notes.txt\n"
        task = self.start()
        os.environ.pop("FAKE_EXTRA")
        os.environ["FAKE_AGENT_SCENARIO"] = "idle"

        status = self.loop(task, "approve", "--rounds", "1")

        task = self.only_task()
        self.assertEqual((status, task["stop_reason"]), (1, "rounds_exhausted"))
        self.assertNotIn("review", [run["role"] for run in task["runs"]])

    def test_changing_a_protected_test_fails_the_guards(self) -> None:
        task = self.start()
        task.update(protected_tests=["hello.txt"], protected_commit=_head(Path(task["worktree"])))
        (self.home / ".hearth/tasks" / task["id"] / "task.json").write_text(json.dumps(task), encoding="utf-8")

        os.environ["FAKE_AGENT_SCENARIO"] = "rewrite"
        self.loop(task, "elsewhere,approve", "--rounds", "1")

        task = self.only_task()
        guards = (self.home / ".hearth/tasks" / task["id"] / "runs/03-fix-claude/guards.txt").read_text(encoding="utf-8")
        self.assertIn("protected test changed: hello.txt", guards)
        self.assertEqual(task["stop_reason"], "rounds_exhausted")


def _head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


class PersonRulesTests(VerifyTestCase):
    """The person's rules for the orchestrator, each held by a test."""

    def snapshot(self) -> dict:
        return {path.relative_to(self.repo): path.read_bytes() for path in self.repo.rglob("*")
                if path.is_file() and ".git" not in path.relative_to(self.repo).parts}

    def test_a_full_task_and_loop_leave_the_main_checkout_untouched(self) -> None:
        before = (self.snapshot(), git(self.repo, "status", "--porcelain"), git(self.repo, "rev-parse", "HEAD"))

        self.run_loop("failed,verified", "changes,approve")

        self.assertEqual((self.snapshot(), git(self.repo, "status", "--porcelain"), git(self.repo, "rev-parse", "HEAD")), before)

    def test_the_workbench_never_merges(self) -> None:
        package = Path(__file__).resolve().parents[1] / "src/hearth/workbench"
        merges = [f"{path.name}:{node.lineno}" for path in package.glob("*.py")
                  for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.split()[:1] == ["merge"]]

        self.assertEqual(merges, [])

    def test_discard_refuses_to_destroy_uncommitted_work(self) -> None:
        task = self.start()
        worktree = Path(task["worktree"])
        (worktree / "hello.txt").write_text("the person's unsaved idea\n", encoding="utf-8")
        (worktree / "draft.txt").write_text("new and uncommitted\n", encoding="utf-8")

        _, preview = self.cli("task", "discard", task["id"])
        refused, _ = self.cli("task", "discard", task["id"], "--apply")

        self.assertIn("hello.txt", preview)
        self.assertIn("draft.txt", preview)
        self.assertEqual(refused, 1)
        self.assertEqual((worktree / "draft.txt").read_text(encoding="utf-8"), "new and uncommitted\n")
        self.assertEqual(self.cli("task", "discard", task["id"], "--apply", "--discard-uncommitted")[0], 0)
        self.assertFalse(worktree.exists())

    def test_every_task_prompt_asks_for_faithful_reports(self) -> None:
        task = self.start()

        prompt = (self.home / ".hearth/tasks" / task["id"] / "runs/01-implement-claude/prompt.md").read_text(encoding="utf-8")

        self.assertIn("never claim a check you did not run", prompt)

    def test_leftover_evidence_is_moved_to_the_receipts_not_deleted(self) -> None:
        task = self.start()
        (Path(task["worktree"]) / ".hearth/evidence").mkdir(parents=True)
        (Path(task["worktree"]) / ".hearth/evidence/old.txt").write_text("from a crashed run\n", encoding="utf-8")
        os.environ["FAKE_VERIFY"] = "verified"

        self.assertEqual(self.loop(task, "approve"), 0)

        kept = list((self.home / ".hearth/tasks" / task["id"]).glob("leftover-evidence-*/old.txt"))
        self.assertEqual([path.read_text(encoding="utf-8") for path in kept], ["from a crashed run\n"])
        self.assertFalse((Path(task["worktree"]) / ".hearth/evidence").exists())


class TestsFirstTests(LoopTestCase):
    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.dict(tasks.PROVIDERS, {"codex": FAKE_CODEX})
        patch.start()
        self.addCleanup(patch.stop)

    def new(self, *argv: str, check: str = "test -f hello.txt", providers: tuple[str, ...] = ("claude", "codex")) -> tuple[int, dict]:
        self.write_projects(check=check, providers=providers)
        status, _ = self.cli("task", "new", "demo", "Add hello.txt", "--tests-first", *argv)
        return status, self.only_task()

    def test_another_family_writes_failing_tests_that_the_implementer_must_pass_unchanged(self) -> None:
        status, task = self.new()

        self.assertEqual((status, task["status"]), (0, "done"))
        self.assertEqual([(run["role"], run["provider"]) for run in task["runs"]], [("test", "codex"), ("implement", "claude")])
        self.assertEqual(task["protected_tests"], ["tests/test_hello.txt"])
        task_dir = self.home / ".hearth/tasks" / task["id"]
        self.assertNotIn("exit code: 0", (task_dir / "runs/01-test-codex/checks.txt").read_text(encoding="utf-8"))
        self.assertIn("tests/test_hello.txt", (task_dir / "runs/02-implement-claude/prompt.md").read_text(encoding="utf-8"))

    def test_tests_that_already_pass_are_rejected(self) -> None:
        status, task = self.new(check="true")

        self.assertEqual((status, task["stop_reason"], len(task["runs"])), (1, "tests_already_pass", 1))

    def test_a_test_run_that_changes_source_is_rejected(self) -> None:
        os.environ["FAKE_TESTS"] = "source"

        status, task = self.new()

        self.assertEqual((status, task["stop_reason"]), (1, "tests_touched_source"))

    def test_approve_tests_waits_for_the_person_before_implementing(self) -> None:
        status, task = self.new("--approve-tests")
        self.assertEqual((status, task["status"], task["stop_reason"], len(task["runs"])), (0, "waiting", "tests_to_approve", 1))
        _, shown = self.cli("task", "show", task["id"])
        self.assertIn("tests/test_hello.txt", shown)

        approved, _ = self.cli("task", "approve-tests", task["id"])

        task = self.only_task()
        self.assertEqual((approved, task["status"], [run["role"] for run in task["runs"]]), (0, "done", ["test", "implement"]))

    def test_tests_first_needs_a_test_writer_from_another_family(self) -> None:
        self.write_projects(check="test -f hello.txt", providers=("claude",))

        status, _ = self.cli("task", "new", "demo", "Add hello.txt", "--tests-first")

        self.assertEqual(status, 1)
        self.assertFalse((self.home / ".hearth/tasks").exists())


class EffortTests(LoopTestCase):
    def first_event(self, task: dict, run: str) -> dict:
        path = self.home / ".hearth/tasks" / task["id"] / "runs" / run / "events.jsonl"
        return json.loads(path.read_text(encoding="utf-8").splitlines()[0])

    def test_each_role_runs_at_its_default_effort_and_records_what_was_used(self) -> None:
        task = self.start()
        self.loop(task, "approve")

        task = self.only_task()
        implement, review = task["runs"]
        self.assertEqual((implement["effort"], review["effort"]), ("medium", "high"))
        self.assertIn("--effort", self.first_event(task, "01-implement-claude")["argv"])
        self.assertIn("model_reasoning_effort=high", self.first_event(task, "02-review-codex")["argv"])
        self.assertEqual(implement["model_used"], "fake-model")

    def test_checking_roles_run_on_pinned_models_and_implementation_on_the_default(self) -> None:
        task = self.start()
        self.loop(task, "approve")

        task = self.only_task()
        implement, review = task["runs"]
        self.assertIsNone(implement["model"])
        self.assertEqual(review["model"], "gpt-6-luna")
        self.assertIn("gpt-6-luna", self.first_event(task, "02-review-codex")["argv"])

    def test_an_explicit_effort_overrides_the_implement_default(self) -> None:
        self.write_projects(check="test -f hello.txt", providers=("claude", "codex"))

        self.cli("task", "new", "demo", "Add hello.txt", "--effort", "low")

        self.assertEqual(self.only_task()["runs"][0]["effort"], "low")

    def test_a_codex_implementer_records_its_configured_default_model(self) -> None:
        (self.home / ".codex").mkdir()
        (self.home / ".codex/config.toml").write_text('model = "gpt-test"\nmodel_reasoning_effort = "medium"\n', encoding="utf-8")
        self.write_projects(check="test -f hello.txt", providers=("claude", "codex"))

        with mock.patch.dict(tasks.PROVIDERS, {"codex": FAKE_CODEX}):
            self.cli("task", "new", "demo", "Add hello.txt", "--agent", "codex")

        self.assertEqual(self.only_task()["runs"][0]["model_used"], "gpt-test")

    def test_review_eval_runs_at_the_chosen_effort_and_antigravity_picks_the_matching_model(self) -> None:
        os.environ["FAKE_REVIEWS"] = ",".join(["approve"] * 6)
        cases = Path(__file__).parent / "fixtures/public/reviews"

        self.cli("review-eval", str(cases), "--reviewer", "antigravity", "--effort", "low")

        rows = [json.loads(line) for line in (self.home / ".hearth/evals/reviews.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({(row["effort"], row["model"]) for row in rows}, {("low", "gemini-3.1-pro-low")})


class RetroTests(LoopTestCase):
    def escaped(self) -> dict:
        task = self.start()
        self.loop(task, "approve")
        self.assertEqual(self.cli("task", "escape", task["id"], "hello.txt never says the person's name.")[0], 0)
        return self.only_task()

    def test_an_escape_is_recorded_on_the_task_and_shown(self) -> None:
        task = self.escaped()

        _, shown = self.cli("task", "show", task["id"])

        self.assertEqual([escape["text"] for escape in task["escapes"]], ["hello.txt never says the person's name."])
        self.assertIn("escape    1: hello.txt never says the person's name.", shown)

    def test_a_retro_proposes_one_change_per_escape_and_prints_the_command_to_approve_it(self) -> None:
        task = self.escaped()

        status, output = self.cli("task", "retro", task["id"])

        task = self.only_task()
        self.assertEqual((status, task["status"]), (0, "done"))
        self.assertEqual((task["runs"][-1]["role"], task["runs"][-1]["provider"]), ("retro", "codex"))
        self.assertEqual(task["escapes"][0]["proposal"]["kind"], "test")
        self.assertIn("hearth task new demo 'Add a regression test that hello.txt greets by name.' --tests-first", output)
        prompt = (self.home / ".fake-last-retro-prompt").read_text(encoding="utf-8")
        self.assertIn("hello.txt never says the person's name.", prompt)
        self.assertIn("02 review", prompt)

    def test_a_guard_proposal_is_approved_as_a_task_on_hearth_itself(self) -> None:
        task = self.escaped()
        os.environ["FAKE_RETRO"] = "guard"

        _, output = self.cli("task", "retro", task["id"])

        self.assertIn("proposal (guard", output)
        self.assertIn("hearth task new hearth 'Add a regression test that hello.txt greets by name.' --tests-first", output)
        prompt = (self.home / ".fake-last-retro-prompt").read_text(encoding="utf-8")
        self.assertIn("a guard in Hearth's own code", prompt)
        self.assertIn("already covers", prompt)

    def test_approving_a_stored_proposal_starts_its_tests_first_task(self) -> None:
        task = self.escaped()
        self.cli("task", "retro", task["id"])

        with mock.patch.dict(tasks.PROVIDERS, {"codex": FAKE_CODEX}):
            status, _ = self.cli("task", "retro", task["id"], "--approve", "1")

        new = json.loads(max((self.home / ".hearth/tasks").glob("*/task.json")).read_text(encoding="utf-8"))
        self.assertEqual(status, 0)
        self.assertEqual(new["goal"], "Add a regression test that hello.txt greets by name.")
        self.assertEqual(new["runs"][0]["role"], "test")
        self.assertEqual(self.only_task_by_id(task["id"])["escapes"][0]["approved_as"], new["id"])

    def test_a_retro_works_after_the_worktree_is_discarded(self) -> None:
        task = self.escaped()
        self.cli("task", "discard", task["id"], "--apply")

        status, _ = self.cli("task", "retro", task["id"])

        self.assertEqual(status, 0)
        self.assertIn(f"+# Task {task['id']}", (self.home / ".fake-last-retro-prompt").read_text(encoding="utf-8"))
        # Read-only in the main checkout, so it sees the project's current standards and guards.
        self.assertEqual(Path((self.home / ".fake-last-retro-cwd").read_text(encoding="utf-8")).resolve(), self.repo.resolve())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_an_unreadable_retro_is_reported_without_a_proposal(self) -> None:
        task = self.escaped()
        os.environ["FAKE_RETRO"] = "garbage"

        status, _ = self.cli("task", "retro", task["id"])

        self.assertEqual(status, 1)
        self.assertNotIn("proposal", self.only_task()["escapes"][0])

    def test_a_retro_needs_an_escape(self) -> None:
        task = self.start()

        self.assertEqual(self.cli("task", "retro", task["id"])[0], 1)


class PullRequestTests(LoopTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pushed: list[str] = []
        self.gh: list[list[str]] = []
        self.gh_replies: dict[str, str] = {"list": "[]", "create": "https://github.com/person/demo/pull/7\n"}
        patches = [
            mock.patch.object(tasks, "_push", side_effect=lambda task: self.pushed.append(task["branch"])),
            mock.patch.object(tasks, "_gh", side_effect=self.fake_gh),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fake_gh(self, args: list[str], cwd: Path) -> str:
        self.gh.append(args)
        key = "run" if args[0] == "run" else args[1]
        return self.gh_replies.get(key, "")

    def start_pr_project(self, pr: bool = True) -> dict:
        self.write_projects(check="test -f hello.txt", providers=("claude", "codex"))
        config = self.home / ".hearth/projects.json"
        projects = json.loads(config.read_text(encoding="utf-8"))
        projects["demo"]["pr"] = pr
        config.write_text(json.dumps(projects), encoding="utf-8")
        self.cli("task", "new", "demo", "Add hello.txt")
        return self.only_task()

    def created(self) -> list[str]:
        return next(args for args in self.gh if args[:2] == ["pr", "create"])

    def test_an_approved_task_is_pushed_and_opened_as_a_draft_following_del_7(self) -> None:
        task = self.start_pr_project()

        self.assertEqual(self.loop(task, "approve"), 0)

        args = self.created()
        title, body = args[args.index("--title") + 1], args[args.index("--body") + 1]
        self.assertEqual(self.pushed, [task["branch"]])
        self.assertIn("--draft", args)
        self.assertEqual(title, "feat: add hello.txt")
        sections = ["## Why", "## What changed", "## Risk", "## Checks"]
        self.assertEqual(sorted(sections, key=body.index), sections)
        self.assertIn("## Why\nAdd hello.txt\n", body)
        self.assertIn("## What changed\nhello.txt now greets the person.\nFiles: hello.txt\n", body)
        self.assertIn("## Risk\nlow: only adds hello.txt (codex)\n", body)
        self.assertIn("- Reviewed: approved by codex (gpt-6-luna, high)", body)
        self.assertIn("- CI: runs on this pull request", body)
        self.assertIn(f"Built by Hearth task {task['id']}: implement claude, review codex.", body)
        self.assertNotIn(str(self.home), body)
        self.assertEqual(self.only_task()["pr"]["url"], "https://github.com/person/demo/pull/7")
        _, shown = self.cli("task", "show", task["id"])
        self.assertIn("pull req  https://github.com/person/demo/pull/7 (draft)", shown)
        self.assertNotIn("gh pr create", shown)

    def test_the_body_credits_the_reviewer_that_approved_not_one_that_fell_back(self) -> None:
        self.write_projects(check="test -f hello.txt", providers=("claude", "codex", "antigravity"))
        config = self.home / ".hearth/projects.json"
        projects = json.loads(config.read_text(encoding="utf-8"))
        projects["demo"]["pr"] = True
        config.write_text(json.dumps(projects), encoding="utf-8")
        self.cli("task", "new", "demo", "Add hello.txt")

        self.loop(self.only_task(), "garbage,approve")

        body = self.created()[self.created().index("--body") + 1]
        self.assertIn("implement claude, review codex.", body)

    def test_a_branch_that_conflicts_with_main_is_not_published(self) -> None:
        task = self.start_pr_project()
        (self.repo / "hello.txt").write_text("main's own hello\n", encoding="utf-8")
        git(self.repo, "add", "hello.txt")
        git(self.repo, "commit", "-q", "-m", "main changes hello.txt")

        status = self.loop(task, "approve")

        self.assertEqual((status, self.pushed), (1, []))
        self.assertEqual([args for args in self.gh if args[:2] == ["pr", "create"]], [])

    def test_projects_without_pr_are_never_pushed(self) -> None:
        self.loop(self.start_pr_project(pr=False), "approve")

        self.assertEqual((self.pushed, self.gh), ([], []))

    def test_what_the_verifier_could_not_check_is_listed_under_risk(self) -> None:
        (self.repo / "VERIFY.md").write_text("Run the program.\n", encoding="utf-8")
        git(self.repo, "add", "VERIFY.md")
        git(self.repo, "commit", "-q", "-m", "add VERIFY.md")
        with mock.patch.dict(tasks.VERIFIERS, {"codex": FAKE_CODEX}):
            self.loop(self.start_pr_project(), "approve")

        body = self.created()[self.created().index("--body") + 1]
        self.assertIn("Not checked: the web interface: no loopback port here", body)
        self.assertIn("- Verified: 1 claim with evidence from codex (", body)

    def test_a_rerun_edits_the_open_pull_request_instead_of_opening_another(self) -> None:
        self.gh_replies["list"] = '[{"number": 7, "url": "https://github.com/person/demo/pull/7"}]'

        self.loop(self.start_pr_project(), "approve")

        self.assertEqual([args[:2] for args in self.gh if args[0] == "pr"], [["pr", "list"], ["pr", "edit"]])

    def test_a_missing_title_falls_back_to_chore_and_says_so(self) -> None:
        os.environ["FAKE_TITLE"] = "none"

        self.loop(self.start_pr_project(), "approve")

        args = self.created()
        self.assertEqual(args[args.index("--title") + 1], "chore: Add hello.txt")
        self.assertIn("gave no valid PR title", args[args.index("--body") + 1])

    def test_a_home_path_in_the_pull_request_stops_publishing(self) -> None:
        self.write_projects(check="test -f hello.txt", providers=("claude", "codex"))
        config = self.home / ".hearth/projects.json"
        projects = json.loads(config.read_text(encoding="utf-8"))
        projects["demo"]["pr"] = True
        config.write_text(json.dumps(projects), encoding="utf-8")
        self.cli("task", "new", "demo", "Add hello.txt like /Users/someone/notes.txt")

        self.loop(self.only_task(), "approve")

        self.assertEqual((self.pushed, [args for args in self.gh if args[:2] == ["pr", "create"]]), ([], []))

    def test_ci_marks_the_pull_request_ready_when_every_check_passes(self) -> None:
        task = self.start_pr_project()
        self.loop(task, "approve")
        self.gh_replies["checks"] = '[{"name": "check", "bucket": "pass", "link": "https://github.com/p/d/actions/runs/5/job/6"}]'

        status, _ = self.cli("task", "ci", task["id"])

        self.assertEqual(status, 0)
        self.assertIn(["pr", "ready", "7"], self.gh)

    def test_ci_waits_while_checks_are_pending(self) -> None:
        task = self.start_pr_project()
        self.loop(task, "approve")
        self.gh_replies["checks"] = '[{"name": "check", "bucket": "pending", "link": ""}]'

        status, output = self.cli("task", "ci", task["id"])

        self.assertEqual(status, 0)
        self.assertNotIn(["pr", "ready", "7"], self.gh)
        self.assertIn("pending", output)

    def test_a_failed_ci_run_becomes_a_fix_run_with_the_failing_log(self) -> None:
        task = self.start_pr_project()
        self.loop(task, "approve")
        self.gh_replies["checks"] = '[{"name": "check", "bucket": "fail", "link": "https://github.com/p/d/actions/runs/55/job/66"}]'
        self.gh_replies["run"] = "FAIL: test_hello (tests.HelloTests)\nAssertionError: expected a greeting\n"

        status, _ = self.cli("task", "ci", task["id"])

        task = self.only_task()
        self.assertEqual(task["runs"][-1]["role"], "fix")
        self.assertIn(["run", "view", "55", "--log-failed"], self.gh)
        fix_prompt = (self.home / ".hearth/tasks" / task["id"] / "runs" / task["runs"][-1]["dir"] / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("AssertionError: expected a greeting", fix_prompt)
        self.assertNotIn(["pr", "ready", "7"], self.gh)


class VerifyEvaluationTests(LoopTestCase):
    CASES = Path(__file__).parent / "fixtures/public/reviews"

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.dict(tasks.VERIFIERS, {"codex": FAKE_CODEX})
        patch.start()
        self.addCleanup(patch.stop)

    def test_every_case_says_whether_its_stated_behavior_holds(self) -> None:
        expects = {path.parent.name: json.loads(path.read_text(encoding="utf-8")).get("verify_expect") for path in self.CASES.glob("*/case.json")}

        self.assertEqual(expects, {"bug-fraction": "failed", "bug-negative": "failed", "bug-skip": "verified",
                                   "bug-swallow": "verified", "clean-days": "verified", "clean-rename": "verified"})

    def test_each_verifier_case_is_scored_and_appended_to_the_evaluation_log(self) -> None:
        # Name order: bug-fraction, bug-negative, bug-skip, bug-swallow, clean-days, clean-rename.
        os.environ["FAKE_VERIFY"] = "failed,verified,verified,missing,verified,garbage"

        status, output = self.cli("verify-eval", str(self.CASES), "--verifier", "codex", "--effort", "low")

        rows = [json.loads(line) for line in (self.home / ".hearth/evals/verifies.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(status, 0)
        self.assertEqual([(row["case"], row["outcome"], row["correct"]) for row in rows], [
            ("bug-fraction", "failed", True), ("bug-negative", "verified", False), ("bug-skip", "verified", True),
            ("bug-swallow", "rejected", False), ("clean-days", "verified", True), ("clean-rename", "rejected", False),
        ])
        self.assertEqual({(row["verifier"], row["effort"]) for row in rows}, {("codex", "low")})
        self.assertIn("broken claims caught: 1 of 2", output)
        self.assertIn("working claims verified: 2 of 4", output)
        self.assertIn("VERIFY.md", (self.home / ".fake-last-verify-prompt").read_text(encoding="utf-8"))


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
