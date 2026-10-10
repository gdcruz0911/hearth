from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from hearth import install, maintenance, update
from hearth.service import HearthService

REPO = Path(__file__).parents[1]
SRC = REPO / "src"
DEV_PYTHON = sys.executable

# Run inside the fake runtime, as the new release's migration would, when FAKE_RUNTIME_CRASH is set: it changes the live
# database enough to spill to disk, leaving a hot journal, then kills the updater and itself mid-migration.
CRASH = textwrap.dedent("""
    import os, signal, sqlite3, sys
    database = os.environ["FAKE_RUNTIME_DATABASE"]
    live = sqlite3.connect(database, isolation_level=None)
    live.execute("PRAGMA cache_size = 1")
    live.execute("BEGIN")
    live.execute("CREATE TABLE half_migrated (x TEXT)")
    live.executemany("INSERT INTO half_migrated VALUES (?)", [("x" * 500,)] * 2000)
    os.kill(os.getppid(), signal.SIGKILL)
    os._exit(9)
""")


class UpdateTests(unittest.TestCase):
    """ADR-0038 and ADR-0040: an update changes nothing while work runs, and is undone exactly when it fails or dies."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.hearth = self.root / "hearth"
        (self.root / "home").mkdir()
        patch = mock.patch.dict(os.environ, {"HOME": str(self.root / "home"), "HEARTH_HOME": str(self.hearth),
                                             "PATH": "/usr/bin:/bin", "PYTHONPATH": str(SRC)})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("HEARTH_TASK", None)
        # The person's data: a knowledge base their profile names through a link, a setting, and a finished task.
        self.database = self.root / "data/hearth.sqlite"
        self.database.parent.mkdir()
        note = self.root / "note.md"
        note.write_text("# Note\n\nThe owner is Ada.\n", encoding="utf-8")
        service = HearthService(self.database)
        service.import_document(str(note))
        service.close()
        kept = self.root / "data/hearth.json"
        kept.write_text(json.dumps({"format": "hearth-runtime-profile-v1", "database": str(self.database)}), encoding="utf-8")
        self.hearth.mkdir()
        (self.hearth / "profile.json").symlink_to(kept)
        (self.hearth / "projects.json").write_text("{}", encoding="utf-8")
        self.task("20261010-090000", "done")
        sign_in = self.hearth / "tasks/20261010-090000/codex-home"
        sign_in.mkdir()
        (sign_in / "auth.json").symlink_to(self.root / "home/.codex/auth.json")
        self.runtimes = self.root / "runtime"
        self.old, self.new = self.fake_runtime("v1"), self.fake_runtime("v2")
        (self.runtimes / "current").symlink_to("v1")

    def task(self, task_id: str, status: str, **extra: object) -> Path:
        folder = self.hearth / "tasks" / task_id
        folder.mkdir(parents=True)
        record = {"id": task_id, "project": "demo", "goal": "Add hello.txt", "status": status, "stop_reason": None, "runs": [],
                  "created": "2026-10-10T09:00:00", "finished": None, "branch": f"hearth/{task_id}", "base": "0" * 40,
                  "worktree": str(self.hearth / "worktrees/demo" / task_id), "format": 1}
        (folder / "task.json").write_text(json.dumps({**record, **extra}), encoding="utf-8")
        return folder

    def fake_runtime(self, name: str) -> Path:
        """A runtime folder whose python is the development one, so every step runs the real CLI without building."""
        folder = self.runtimes / name
        (folder / "bin").mkdir(parents=True)
        python = folder / "bin/python"
        python.write_text("#!/bin/sh\n"
                          f'case "$*" in *--maintenance-fd*" list"*) if [ -n "$FAKE_RUNTIME_CRASH" ]; then exec "{DEV_PYTHON}" -c \'{CRASH}\'; fi;; esac\n'
                          f'exec "{DEV_PYTHON}" "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        (folder / "runtime.json").write_text(json.dumps({"format": "hearth-runtime-v1", "base": {"path": DEV_PYTHON, "version": "3"},
                                                         "ref": name}), encoding="utf-8")
        return folder

    def run_update(self) -> str:
        return update.update(REPO, "v2", None, self.runtimes, runtime=self.new)

    def documents(self) -> int:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute("SELECT count(*) FROM documents").fetchone()[0]
        finally:
            connection.close()

    def digest(self, path: Path) -> str:
        """The database's content: a restore through SQLite's backup brings back every row, not the same header bytes."""
        connection = sqlite3.connect(path)
        try:
            return hashlib.sha256("\n".join(connection.iterdump()).encode()).hexdigest()
        finally:
            connection.close()

    def unchanged(self, database_before: str) -> None:
        self.assertEqual(self.digest(self.database), database_before)
        self.assertEqual(os.readlink(self.runtimes / "current"), "v1")
        self.assertFalse((self.hearth / update.MARKER).exists())

    def test_an_update_backs_up_checks_and_switches_and_leaves_the_lock_free(self) -> None:
        said = self.run_update()

        self.assertIn("Updated to v2", said)
        self.assertEqual(os.readlink(self.runtimes / "current"), "v2")
        self.assertFalse((self.hearth / update.MARKER).exists())
        (backup,) = (self.hearth / update.BACKUPS).iterdir()
        copy = sqlite3.connect(backup / "hearth.sqlite")
        self.assertEqual(copy.execute("SELECT count(*) FROM documents").fetchone()[0], 1)
        copy.close()
        self.assertTrue((backup / "home/profile.json").is_symlink())  # Kept a link, so the two profiles never drift apart.
        self.assertTrue((backup / "home/tasks/20261010-090000/task.json").is_file())
        self.assertFalse((backup / "home/tasks/20261010-090000/codex-home").exists())  # The person's sign-in is never copied.
        with maintenance.writing(self.hearth):
            pass  # Released: writers run again.

    def test_work_under_way_stops_the_update_before_anything_changes(self) -> None:
        self.task("20261010-090100", "waiting", stop_reason="question")
        before = self.digest(self.database)

        with self.assertRaisesRegex(install.InstallError, r"20261010-090100 is waiting \(question\)\. Next:"):
            self.run_update()

        self.unchanged(before)
        self.assertFalse((self.hearth / update.BACKUPS).exists())

    def test_an_open_app_or_a_running_command_stops_the_update(self) -> None:
        before = self.digest(self.database)
        with maintenance.writing(self.hearth):  # As the app's backend holds it for as long as it runs.
            with self.assertRaisesRegex(install.InstallError, "quit the Hearth app"):
                self.run_update()

        self.unchanged(before)

    def test_a_failure_at_any_step_is_undone(self) -> None:
        before = self.digest(self.database)
        for name in ("_inside", "_trial_backend", "switch"):
            with self.subTest(step=name):
                target = install if name == "switch" else update
                with mock.patch.object(target, name, side_effect=RuntimeError(f"{name} failed")):
                    with self.assertRaisesRegex(install.InstallError, f"undone[\\s\\S]*{name} failed"):
                        self.run_update()
                self.unchanged(before)

    def test_a_database_newer_than_the_release_is_refused_and_left_as_it_was(self) -> None:
        sqlite3.connect(self.database).execute("PRAGMA user_version = 99").connection.close()
        before = self.digest(self.database)

        with self.assertRaisesRegex(install.InstallError, "migrated by a newer Hearth"):
            self.run_update()

        self.unchanged(before)

    def updater_killed(self, **env: str) -> None:
        """Run an update in its own process, which dies with SIGKILL partway, as after a crash or a forced quit."""
        program = (f"from pathlib import Path\nfrom hearth import update\n"
                   f"update.update(Path({str(REPO)!r}), 'v2', None, Path({str(self.runtimes)!r}), runtime=Path({str(self.new)!r}))")
        died = subprocess.run([DEV_PYTHON, "-c", program], env={**os.environ, **env}, capture_output=True, text=True, timeout=120)
        self.assertEqual(died.returncode, -signal.SIGKILL, died.stderr)

    def test_an_updater_killed_mid_migration_is_undone_by_recover_through_sqlite(self) -> None:
        documents = self.documents()
        self.updater_killed(FAKE_RUNTIME_CRASH="1", FAKE_RUNTIME_DATABASE=str(self.database))
        self.assertTrue(Path(f"{self.database}-journal").exists())  # The crash left a hot journal beside the live database.

        with self.assertRaisesRegex(maintenance.Held, r"interrupted[\s\S]*hearth\.install recover"):
            with maintenance.writing(self.hearth):
                pass  # Nothing writes until the update is recovered.
        said = update.recover()

        self.assertIn("undone", said)
        connection = sqlite3.connect(self.database)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = 'half_migrated'").fetchone())
        connection.close()
        self.assertEqual(self.documents(), documents)
        self.assertEqual(os.readlink(self.runtimes / "current"), "v1")
        self.assertFalse((self.hearth / update.MARKER).exists())

    def test_an_updater_killed_mid_backup_changed_nothing_and_recover_only_clears_it(self) -> None:
        before = self.digest(self.database)
        program_env = {"PYTHONSTARTUP": ""}
        died = subprocess.run([DEV_PYTHON, "-c", textwrap.dedent(f"""\
            import os, shutil, signal
            from pathlib import Path
            from hearth import update
            shutil.copytree = lambda *args, **kwargs: os.kill(os.getpid(), signal.SIGKILL)
            update.update(Path({str(REPO)!r}), 'v2', None, Path({str(self.runtimes)!r}), runtime=Path({str(self.new)!r}))
            """)], env={**os.environ, **program_env}, capture_output=True, text=True, timeout=120)
        self.assertEqual(died.returncode, -signal.SIGKILL, died.stderr)
        self.assertEqual(update._marker(self.hearth)["step"], "backing-up")

        said = update.recover()

        self.assertIn("had not changed anything", said)
        self.unchanged(before)
        self.assertEqual(list((self.hearth / update.BACKUPS).glob(".partial-*")), [])

    def test_recover_refuses_and_deletes_nothing_when_the_backup_is_missing(self) -> None:
        update._write_marker(self.hearth, {"step": "changing", "backup": str(self.root / "gone"), "root": str(self.runtimes),
                                           "current_before": "v1", "database": str(self.database)})

        with self.assertRaisesRegex(install.InstallError, "missing or incomplete"):
            update.recover()

        self.assertTrue((self.hearth / update.MARKER).exists())

    def test_a_dashboard_resume_the_update_refused_is_continued_after_it(self) -> None:
        folder = self.task("20261010-090200", "done")
        gone = subprocess.Popen([DEV_PYTHON, "-c", "pass"])
        gone.wait()
        (folder / "resume.json").write_text(json.dumps({"pid": gone.pid, "identity": None, "command": "loop"}), encoding="utf-8")
        (folder / f"refused-{gone.pid}.json").write_text(json.dumps({"command": "loop"}), encoding="utf-8")

        said = self.run_update()

        self.assertIn("Continued task 20261010-090200.", said)
        pid = json.loads((folder / "resume.json").read_text(encoding="utf-8"))["pid"]
        self.assertNotEqual(pid, gone.pid)
        for _ in range(200):  # The continuation fails fast here, with no project; let it end before the folder goes.
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)

    def test_one_update_at_a_time(self) -> None:
        self.hearth.mkdir(exist_ok=True)
        with (self.hearth / "update.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaisesRegex(install.InstallError, "Another Hearth update is running"):
                self.run_update()
            with self.assertRaisesRegex(install.InstallError, "Another Hearth update is running"):
                update.recover()


if __name__ == "__main__":
    unittest.main()
