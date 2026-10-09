from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearth import cli, maintenance


def commands() -> set[str]:
    """Every command and subcommand the CLI parses, such as "task new"."""
    captured = {}

    def grab(self, argv=None, namespace=None):
        captured["parser"] = self
        raise SystemExit(0)

    with mock.patch.object(argparse.ArgumentParser, "parse_args", grab), contextlib.suppress(SystemExit):
        cli.main(["--help"])
    names = set()

    def walk(parser: argparse.ArgumentParser, prefix: str) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    names.add(f"{prefix} {name}".strip())
                    walk(sub, f"{prefix} {name}")

    walk(captured["parser"], "")
    return names


class MaintenanceLockTests(unittest.TestCase):
    """ADR-0038: while an update holds the maintenance lock, every command and backend that writes refuses."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.home = Path(self.temporary_directory.name)
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("HEARTH_TASK", None)
        os.environ.pop("HEARTH_HOME", None)  # Restored by the patch; a set HEARTH_HOME would move these records.

    def updating(self) -> None:
        path = self.home / ".hearth/maintenance.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a")
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def cli(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
            try:
                status = cli.main(list(argv))
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def test_every_read_only_command_exists_and_everything_else_writes(self) -> None:
        names = commands()
        self.assertLessEqual(maintenance.READ_ONLY, names)  # A renamed command would otherwise turn into a silent exception.
        for name in names:
            with self.subTest(command=name):
                self.assertEqual(maintenance.writes(name), name not in maintenance.READ_ONLY)

    def test_writers_refuse_and_change_nothing_while_an_update_holds_the_lock(self) -> None:
        self.updating()
        note = self.home / "note.md"
        note.write_text("# A note\n", encoding="utf-8")
        database = self.home / "hearth.sqlite"
        for argv in (["task", "new", "demo", "Add hello.txt"], ["--database", str(database), "import", str(note)],
                     ["ask", "Who owns deployment?"], ["profile", "create", str(self.home / "profile.json"), "--database", str(database)]):
            with self.subTest(command=" ".join(argv[:2])):
                status, _, err = self.cli(*argv)
                self.assertEqual(status, 1)
                self.assertIn(f"Next: once the update finishes, hearth {shlex.join(argv)}", err)  # CLI-6: the command to retry.
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [".hearth", "note.md"])
        self.assertEqual([path.name for path in (self.home / ".hearth").iterdir()], ["maintenance.lock"])

    def test_read_only_commands_still_run_while_an_update_holds_the_lock(self) -> None:
        self.updating()

        status, out, _ = self.cli("task", "list")

        self.assertEqual(status, 0)

    def test_the_statusline_still_prints_but_records_nothing_during_an_update(self) -> None:
        self.updating()
        status = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 12}}})

        code, out, _ = self.cli("usage", "statusline", stdin=status)

        self.assertEqual(code, 0)
        self.assertTrue(out.strip())
        self.assertFalse((self.home / ".hearth/usage").exists())

    def test_a_writer_holds_the_lock_while_it_runs_so_an_update_cannot_start(self) -> None:
        with maintenance.writing(self.home / ".hearth"):
            handle = (self.home / ".hearth/maintenance.lock").open("a")
            self.addCleanup(handle.close)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_a_knowledge_command_refuses_before_it_opens_the_database(self) -> None:
        self.updating()
        database = self.home / "new.sqlite"

        status, _, err = self.cli("--database", str(database), "list")

        self.assertEqual(status, 1)
        self.assertIn("Next:", err)
        self.assertFalse(database.exists())

if __name__ == "__main__":
    unittest.main()
