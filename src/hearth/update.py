"""Updating the Hearth app's runtime in place of the current one, under ADR-0038 as ADR-0040 orders it.

`update` builds the new runtime first, touching no live data, then refuses while any work is running, takes the maintenance
lock exclusively, rechecks, and only then changes anything: it backs up, lets the new runtime check and migrate the data and
prove its backend starts while the lock holds every writer off, switches, and releases. A durable marker records how far it
got, so `recover` can undo an update that died after live data may have changed, and do nothing when it had not.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from . import install
from .install import InstallError, clean_environment
from .runtime import hearth_home, load_runtime_profile

MARKER = "update.json"
BACKUPS = "backups"
# Hearth's home is backed up whole, except what is not data or would be wrong to restore: the locks (a restored
# maintenance.lock would be another file than the one an update holds), earlier backups, the app's own Electron data, and
# each task's Codex home, which links to the person's sign-in and must never be copied, and the task worktrees, which are
# code each task's branch and checkpoints already keep.
SKIPPED = {"maintenance.lock", "rebuild.lock", "update.lock", MARKER, BACKUPS, "app", "codex-home", "worktrees"}


def update(repo: Path, ref: str, base: str | None, root: Path = install.ROOT, runtime: Path | None = None) -> str:
    """Install `ref` as the current runtime; returns what to tell the person. `runtime` skips building, for tests."""
    home = hearth_home()
    with _only_update(home):
        if (home / MARKER).exists():
            raise InstallError(f"An earlier update was interrupted.\nNext: {_recover_command(_marker(home).get('source'))}")
        new = runtime or _built(repo, ref, base or _current_base(root), root)  # No live data is touched before this is done.
        busy = _busy(home)
        if busy:
            raise InstallError("Hearth will not update while work is under way:\n" + "\n".join(busy))
        lock = _exclusive(home)
        try:
            busy = _busy(home)  # Anything that started between the first check and the lock is found here (ADR-0040).
            if busy:
                raise InstallError("Work started while the update was getting ready, so nothing was changed:\n" + "\n".join(busy))
            database = _database(home)
            before = os.readlink(root / "current") if (root / "current").is_symlink() else None
            _write_marker(home, {"step": "backing-up", "ref": ref, "source": str(repo.resolve()), "root": str(root),
                                 "current_before": before, "database": str(database) if database else None})
            try:
                backup = _backup(home, database, ref)
            except BaseException:
                _discard_marker(home)  # Live data was never changed, so there is nothing to restore.
                raise
            _write_marker(home, {**_marker(home), "step": "changing", "backup": str(backup)})
            try:
                _inside(new, lock, ["task", "list", "--json"])  # Reads every task record; a newer one is refused.
                if database is not None:
                    _inside(new, lock, ["list"])  # Opens the database: migrates it, or refuses one newer than this release.
                    _trial_backend(new, lock)
                install.switch(root / "current", new)
            except BaseException as exc:
                _restore(home)
                raise InstallError(f"The update failed and was undone, with your data as it was before it:\n{exc}") from None
            _discard_marker(home)
        finally:
            os.close(lock)  # Released only now: every child given it has been reaped.
    continued = _continue_refused(new)
    lines = [f"Updated to {ref}: {new}.", f"The backup is in {backup}."]
    lines += [f"Continued task {task}." for task in continued]
    return "\n".join(lines + ["Next: reopen the Hearth app."])


def recover() -> str:
    """Finish an interrupted update: undo it if live data may have changed, and only clear its marker if not."""
    home = hearth_home()
    with _only_update(home):
        if not (home / MARKER).exists():
            return "No update was interrupted; nothing to recover."
        lock = _exclusive(home)
        try:
            marker = _marker(home)
            if marker["step"] != "changing":
                _discard_marker(home)
                return "The interrupted update had not changed anything yet; its partial backup was removed. Your data is as it was."
            _restore(home)
        finally:
            os.close(lock)
    return f"The interrupted update was undone from {marker['backup']}; your data and runtime are as they were before it."


@contextlib.contextmanager
def _only_update(home: Path) -> Iterator[None]:
    """One update or recovery at a time, from building onwards, so two never build or restore over each other."""
    home.mkdir(parents=True, exist_ok=True)
    with (home / "update.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("Another Hearth update is running.\nNext: let it finish, then run this again.") from None
        yield


def _exclusive(home: Path) -> int:
    """The maintenance lock, held exclusively; while the app's backend or any command that writes runs, it cannot be taken."""
    fd = os.open(home / "maintenance.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise InstallError("The Hearth app, or a Hearth command that changes data, is still running.\n"
                           "Next: quit the Hearth app and let any running hearth command finish, then run the update again.") from None
    return fd


def _busy(home: Path) -> list[str]:
    """What the update would cut short, each with what to do. Task states come from `task list --json`, read the way the
    dashboard reads them, so an old crash counts as over; the workbench is reached only through the CLI."""
    listed = subprocess.run([sys.executable, "-m", "hearth.cli", "task", "list", "--json"], capture_output=True, text=True,
                            env=clean_environment(), cwd=Path.home(), timeout=120)
    if listed.returncode != 0:
        return [f"- Hearth could not read its task records: {(listed.stderr or listed.stdout).strip()[-500:]}"]
    found = []
    for task in json.loads(listed.stdout)["tasks"]:
        if task["status"] in ("running", "queued", "waiting"):
            reason = f" ({task['stop_reason']})" if task.get("stop_reason") else ""
            found.append(f"- task {task['id']} is {task['status']}{reason}. Next: let it finish, answer it, or hearth task cancel {task['id']}")
    for record in sorted(home.glob("tasks/*/resume.json")):
        if _alive(json.loads(record.read_text(encoding="utf-8")).get("pid")):  # A reused ID refuses more, never less.
            found.append(f"- task {record.parent.name} is being continued in its own process. Next: let it finish")
    if (home / "rebuild.lock").exists():
        with (home / "rebuild.lock").open("a") as claim:
            try:
                fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                found.append("- an index rebuild is running. Next: let it finish, or cancel it in the Knowledge tab")
    return found


def _alive(pid: object) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _database(home: Path) -> Path | None:
    """The knowledge database the default profile names, or None when there is no profile to update data for."""
    path = home / "profile.json"
    if not (path.exists() or path.is_symlink()):
        return None
    database = load_runtime_profile(path).database
    return database if database is not None and database.is_file() else None


def _backup(home: Path, database: Path | None, ref: str) -> Path:
    """A consistent copy of the database and Hearth's home, put in place only once complete; links are kept, never followed."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")  # Unique, even for two updates a second apart.
    final = home / BACKUPS / f"{stamp}-{''.join(c if c.isalnum() or c in '.-_' else '-' for c in ref)}"
    partial = home / BACKUPS / f".partial-{final.name}"
    partial.mkdir(parents=True)
    shutil.copytree(home, partial / "home", symlinks=True, ignore=lambda folder, names: [name for name in names if name in SKIPPED])
    if database is not None:
        source, copy = sqlite3.connect(database), sqlite3.connect(partial / "hearth.sqlite")
        try:
            source.backup(copy)  # SQLite's own consistent copy, never a copy of a file that may be mid-write.
        finally:
            source.close()
            copy.close()
    (partial / "complete").write_text(json.dumps({"database": str(database) if database else None}), encoding="utf-8")
    os.sync()
    partial.rename(final)
    return final


def _restore(home: Path) -> None:
    """Put the database and the runtime back as they were before the update; safe to run again if it is interrupted."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)  # A second Ctrl+C must not cut a restore short.
    try:
        marker = _marker(home)
        backup = Path(marker.get("backup") or "")
        if not (backup / "complete").is_file():
            raise InstallError(f"The update's backup at {backup} is missing or incomplete, so Hearth will not restore from it and has deleted nothing.\n"
                               f"Next: keep {home} as it is and ask for help before running Hearth.")
        if marker.get("database"):
            # Through SQLite, not by copying the file: opening the live database first rolls back any journal a crash left.
            source, live = sqlite3.connect(backup / "hearth.sqlite"), sqlite3.connect(marker["database"])
            try:
                source.backup(live)
            finally:
                source.close()
                live.close()
        current = Path(marker["root"]) / "current"
        if marker.get("current_before") and (not current.is_symlink() or os.readlink(current) != marker["current_before"]):
            install.switch(current, Path(marker["root"]) / marker["current_before"])
        _discard_marker(home, keep_backup=True)
    finally:
        signal.signal(signal.SIGINT, previous)


def _inside(runtime: Path, lock: int, argv: list[str]) -> None:
    """Run one of the new runtime's commands inside the update's lock, handed to it as ADR-0040 sets out, and wait for it."""
    result = subprocess.run([str(runtime / "bin/python"), "-I", "-m", "hearth.cli", "--maintenance-fd", str(lock), *argv],
                            pass_fds=(lock,), capture_output=True, text=True, env=clean_environment(), cwd=Path.home(), timeout=600)
    if result.returncode != 0:
        raise InstallError(f"The new runtime's `{' '.join(argv)}` failed:\n{(result.stderr or result.stdout).strip()[-2000:]}")


def _trial_backend(runtime: Path, lock: int) -> None:
    """Prove the new backend starts on the person's data inside the lock, then stop and reap it before the lock is released."""
    ours, theirs = socket.socketpair()
    backend = subprocess.Popen([str(runtime / "bin/python"), "-I", "-m", "hearth.cli", "--maintenance-fd", str(lock),
                                "--profile", str(hearth_home() / "profile.json"), "web", "--desktop", "--handshake-fd", str(theirs.fileno())],
                               pass_fds=(lock, theirs.fileno()), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                               env=clean_environment(), cwd=Path.home())
    theirs.close()
    try:
        install.check_handshake(ours, backend)
    finally:
        ours.close()
        backend.terminate()
        backend.communicate(timeout=60)  # Reaped here: it holds the lock's descriptor until it has exited.


def _continue_refused(runtime: Path) -> list[str]:
    """After the update, continue each dashboard answer or approval it refused, through the new runtime (ADR-0040);
    `task resume` continues only those, and refuses every other task that has a resume record."""
    continued = []
    for record in sorted(hearth_home().glob("tasks/*/resume.json")):
        result = subprocess.run([str(runtime / "bin/python"), "-I", "-m", "hearth.cli", "task", "resume", record.parent.name],
                                capture_output=True, text=True, env=clean_environment(), cwd=Path.home(), timeout=120)
        if result.returncode == 0:
            continued.append(record.parent.name)
    return continued


def _built(repo: Path, ref: str, base: str, root: Path) -> Path:
    """The runtime for `ref`, built if it is not already there; one an earlier, refused update built is used again."""
    try:
        return install.install(repo, ref, base, root, make_current=False)
    except install.AlreadyInstalled as exc:
        return exc.target


def _current_base(root: Path) -> str:
    try:
        return json.loads((root / "current" / "runtime.json").read_text(encoding="utf-8"))["base"]["path"]
    except (OSError, ValueError, KeyError, TypeError):
        raise InstallError("Hearth cannot tell which Python the current runtime was built on.\n"
                           "Next: pass --base with the python.org Python, such as /Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14") from None


def _marker(home: Path) -> dict:
    return json.loads((home / MARKER).read_text(encoding="utf-8"))


def _write_marker(home: Path, marker: dict) -> None:
    """Durably, so a crash right after leaves it on disk for `recover`."""
    staged = home / f"{MARKER}.tmp"
    with staged.open("w", encoding="utf-8") as file:
        json.dump(marker, file)
        file.flush()
        os.fsync(file.fileno())
    staged.replace(home / MARKER)
    folder = os.open(home, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def _discard_marker(home: Path, keep_backup: bool = False) -> None:
    if not keep_backup:
        for partial in (home / BACKUPS).glob(".partial-*"):
            shutil.rmtree(partial, ignore_errors=True)
    (home / MARKER).unlink(missing_ok=True)


def _recover_command(source: str | None) -> str:
    return f'{f"cd {json.dumps(source)} && " if source else "in the Hearth checkout, run "}.venv/bin/python -m hearth.install recover'
