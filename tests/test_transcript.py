from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearth.workbench import transcript

PROVIDERS = Path(__file__).with_name("fixtures") / "public/providers"
STREAMS = {"claude": "claude-print-2.1.283.jsonl", "codex": "codex-exec-0.157.1.jsonl", "antigravity": "agy-print-1.2.11.jsonl"}


class TranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        home = Path(self.temporary_directory.name)
        patch = mock.patch.dict(os.environ, {"HOME": str(home)})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("HEARTH_HOME", None)  # Restored by the patch; a set HEARTH_HOME would move these records.
        self.task_dir = home / ".hearth/tasks/20260930-120000"

    def run_with(self, provider: str, events: bytes, finished: bool = True) -> Path:
        run = f"01-implement-{provider}"
        (self.task_dir / "runs" / run).mkdir(parents=True, exist_ok=True)
        record = {"dir": run, "role": "implement", "provider": provider, "pid": None, "finished": "2026-09-30T12:01:00-0400" if finished else None}
        (self.task_dir / "task.json").write_text(json.dumps({"id": "20260930-120000", "status": "done", "runs": [record]}), encoding="utf-8")
        path = self.task_dir / "runs" / run / "events.jsonl"
        path.write_bytes(events)
        return path

    def test_each_providers_recorded_stream_becomes_display_items(self) -> None:
        expected = {"claude": {"start", "message", "result"}, "codex": {"start", "tool", "output", "message"},
                    "antigravity": {"start", "tool", "output", "message", "result"}}
        for provider, name in STREAMS.items():
            with self.subTest(provider=provider):
                path = self.run_with(provider, (PROVIDERS / name).read_bytes())

                shown = transcript.read("20260930-120000", f"01-implement-{provider}", 0)

                self.assertEqual({item["kind"] for item in shown["items"]}, expected[provider])
                self.assertEqual(shown["offset"], path.stat().st_size)
                self.assertTrue(all(isinstance(item["text"], str) for item in shown["items"]))

    def test_a_line_still_being_written_waits_for_the_next_read(self) -> None:
        lines = (PROVIDERS / STREAMS["codex"]).read_bytes().splitlines(keepends=True)
        path = self.run_with("codex", lines[0] + lines[2][:10], finished=False)

        first = transcript.read("20260930-120000", "01-implement-codex", 0)
        with path.open("ab") as events:
            events.write(lines[2][10:])
        second = transcript.read("20260930-120000", "01-implement-codex", first["offset"])

        self.assertEqual([item["kind"] for item in first["items"]], ["start"])
        self.assertEqual(first["offset"], len(lines[0]))
        self.assertEqual([item["kind"] for item in second["items"]], ["tool"])
        self.assertFalse(second["finished"])

    def test_only_a_recorded_run_of_a_real_task_can_be_read(self) -> None:
        self.run_with("claude", b"")

        self.assertIsNone(transcript.read("20260930-999999", "01-implement-claude", 0))
        for task_id, run in (("../tasks", "01-implement-claude"), ("20260930-120000", "../../task.json"), ("20260930-120000", "02-review-codex")):
            with self.subTest(task=task_id, run=run), self.assertRaises(ValueError):
                transcript.read(task_id, run, 0)

    def test_long_text_is_clipped_with_a_note(self) -> None:
        line = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "x" * (transcript.TEXT + 5)}}) + "\n"
        self.run_with("codex", line.encode())

        (item,) = transcript.read("20260930-120000", "01-implement-codex", 0)["items"]

        self.assertTrue(item["text"].endswith("[5 more characters in the run's events.jsonl]"))


class DiffTests(unittest.TestCase):
    """The dashboard's diff pane reads a task's saved diff.patch, for the person only."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        home = Path(self.temporary_directory.name)
        patch = mock.patch.dict(os.environ, {"HOME": str(home)})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("HEARTH_HOME", None)  # Restored by the patch; a set HEARTH_HOME would move these records.
        self.task_dir = home / ".hearth/tasks/20260930-120000"
        self.task_dir.mkdir(parents=True)
        (self.task_dir / "task.json").write_text(json.dumps({"id": "20260930-120000", "status": "done", "runs": []}), encoding="utf-8")

    def test_a_saved_diff_is_returned_with_its_line_counts(self) -> None:
        (self.task_dir / "diff.patch").write_text("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n-old\n+new\n+more\n", encoding="utf-8")

        shown = transcript.diff("20260930-120000")

        self.assertEqual((shown["added"], shown["removed"], shown["truncated"]), (2, 1, False))
        self.assertIn("+more", shown["diff"])

    def test_no_diff_yet_unknown_tasks_and_bad_ids(self) -> None:
        self.assertEqual(transcript.diff("20260930-120000")["diff"], None)
        self.assertIsNone(transcript.diff("20260930-999999"))
        with self.assertRaises(ValueError):
            transcript.diff("../../etc")

    def test_a_large_diff_is_cut_at_a_line_and_says_so(self) -> None:
        (self.task_dir / "diff.patch").write_text("+line\n" * (transcript.tasks.DIFF_LIMIT // 3), encoding="utf-8")

        shown = transcript.diff("20260930-120000")

        self.assertTrue(shown["truncated"])
        self.assertLessEqual(len(shown["diff"].encode()), transcript.tasks.DIFF_LIMIT)
        self.assertTrue(shown["diff"].endswith("\n"))
