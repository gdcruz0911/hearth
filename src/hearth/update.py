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
import filecmp
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

from . import install, maintenance
from .install import InstallError, clean_environment
from .runtime import RuntimeProfileError, hearth_home, load_runtime_profile

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
    with only_update(home):
        if (home / MARKER).exists():
            raise InstallError(f"An earlier update was interrupted.\nNext: {maintenance.recover_command(_marker(home).get('source'))}")
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
                backup = _backup(home, database, ref, before, "update")
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
    continued, failed = _continue_refused(new)
    lines = [f"Updated to {ref}: {new}.", f"The backup is in {backup}."]
    lines += [f"Continued task {task}." for task in continued]
    for task in failed:
        print(f"Task {task} could not be continued. Next: hearth task resume {task}", file=sys.stderr)  # A warning (CLI-1).
    return "\n".join(lines + ["Next: reopen the Hearth app."])


def recover() -> str:
    """Finish an interrupted update: undo it if live data may have changed, and only clear its marker if not."""
    home = hearth_home()
    with only_update(home):
        if not (home / MARKER).exists():
            return "No update was interrupted; nothing to recover."
        lock = _exclusive(home)
        try:
            marker = _marker(home)
            if marker["step"] != "changing":
                _discard_marker(home)
                return f"The interrupted {marker.get('action', 'update')} had not changed anything yet; its partial backup was removed. Your data is as it was."
            _restore(home)
        finally:
            os.close(lock)
    return f"The interrupted {marker.get('action', 'update')} was undone from {marker['backup']}; your data and runtime are as they were before it."


def rollback(repo: Path, root: Path = install.ROOT, name: str | None = None, confirm: bool = False) -> str:
    """Go back to the runtime and data an earlier backup holds (ADR-0038): a preview unless `confirm`, and the current data
    saved as a backup first. Runs nothing from the older runtime, which may predate the commands an update relies on."""
    home = hearth_home()
    with only_update(home):
        if (home / MARKER).exists():
            raise InstallError(f"An earlier update or rollback was interrupted.\nNext: {maintenance.recover_command(_marker(home).get('source'))}")
        target = _chosen(home, name)
        record = _complete(target)
        database = Path(record["database"]) if record.get("database") else None
        if database != _database(home):
            raise InstallError(f"The backup {target.name} holds the database {database}, but your profile now names {_database(home)}, "
                               f"so Hearth will not roll back to it and has changed nothing.\n"
                               f"Next: point {home / 'profile.json'} back at {database}, or choose another backup with --backup")
        problem = _unusable(target, database)
        if problem:
            raise InstallError(f"The backup at {target} {problem}, so Hearth will not roll back to it and has changed nothing.\n"
                               f"Next: choose another backup in {home / BACKUPS} with --backup")
        runtime = record.get("current")
        if not runtime or not (root / runtime).is_dir():
            raise InstallError(f"The runtime the backup {target.name} ran on, {runtime}, is not in {root}, so Hearth will not roll back to it.\n"
                               f"Next: choose another backup in {home / BACKUPS} with --backup")
        before = os.readlink(root / "current") if (root / "current").is_symlink() else None
        if before == runtime:
            raise InstallError(f"Hearth already runs {runtime}, the runtime of the backup {target.name}; rolling back to it again would only "
                               f"erase what changed since.\nNext: choose another backup in {home / BACKUPS} with --backup")
        if not confirm:
            return "\n".join([f"Rolling back to the backup {target.name} would switch the app from {before} to {runtime}.",
                              *_erased(home, target, database),
                              "Your current data would be saved as a new backup first.",
                              f"Next: quit the Hearth app, then run .venv/bin/python -m hearth.install rollback --backup {target.name} --confirm"])
        erased = _erased(home, target, database)  # Named before anything changes, for the record a confirmed rollback leaves.
        busy = _busy(home)
        if busy:
            raise InstallError("Hearth will not roll back while work is under way:\n" + "\n".join(busy))
        lock = _exclusive(home)
        try:
            busy = _busy(home)
            if busy:
                raise InstallError("Work started while the rollback was getting ready, so nothing was changed:\n" + "\n".join(busy))
            _write_marker(home, {"action": "rollback", "step": "backing-up", "source": str(repo.resolve()), "root": str(root),
                                 "current_before": before, "database": str(database) if database else None})
            try:
                saved = _backup(home, database, "before-rollback", before, "before-rollback")
            except BaseException:
                _discard_marker(home)
                raise
            _write_marker(home, {**_marker(home), "step": "changing", "backup": str(saved)})
            try:
                _restore_from(home, target, database, root, runtime)
            except BaseException as exc:
                _restore(home)  # Back to the data just saved, which the marker names.
                raise InstallError(f"The rollback failed and was undone, with your data as it was before it:\n{exc}") from None
            _discard_marker(home)
        finally:
            os.close(lock)
    return (f"Rolled back to {runtime}, with the data from {target.name}.\n" + "\n".join(erased) +
            f"\nYour data from just before is in {saved}.\nTo return to it: .venv/bin/python -m hearth.install rollback --backup {saved.name} --confirm\nNext: reopen the Hearth app.")


def _chosen(home: Path, name: str | None) -> Path:
    """The backup named, or else the newest an update made; a save made before a rollback is chosen only by name."""
    if name:
        target = home / BACKUPS / name
        if Path(name).name != name or _complete(target) is None:
            raise InstallError(f"There is no complete, readable backup named {name} in {home / BACKUPS}.\n"
                               f"Next: choose another one listed there with --backup")
        return target
    made = [complete.parent for complete in (home / BACKUPS).glob("*/complete") if (_complete(complete.parent) or {}).get("kind") == "update"]
    if not made:
        raise InstallError(f"No update has left a backup in {home / BACKUPS} to roll back to.\n"
                           f"Next: name one with --backup, or run .venv/bin/python -m hearth.install update --ref <tag> first")
    return max(made, key=lambda folder: folder.name)  # Named by time first, so the newest sorts last.


def _erased(home: Path, backup: Path, database: Path | None) -> list[str]:
    """What restoring `backup` would erase or put back, in words, for the preview; reads the live data only."""
    then, now = _held(backup / "home"), _held(home, database)
    tasks: dict[str, str] = {}
    files = []
    for name in sorted(now.keys() | then.keys()):
        if name not in then:
            change = "made since"
        elif name not in now:
            change = "removed since"
        elif now[name] != then[name] or (now[name][0] == "file" and not filecmp.cmp(home / name, backup / "home" / name, shallow=False)):
            change = "changed since"
        else:
            continue
        parts = Path(name).parts
        if parts[0] == "tasks" and len(parts) > 1:
            tasks.setdefault(parts[1], change if len(parts) == 2 else "changed since")
        elif (now.get(name) or then[name])[0] != "folder":
            files.append(f"  {name} ({change})")
    lines = [f"  task {task} ({change})" for task, change in sorted(tasks.items())]
    if lines:
        lines = ["Task records it would put back as they were (made since: removed):", *lines]
        if any(change == "made since" for change in tasks.values()):
            lines.append("  Their worktrees, branches and Codex sessions stay; remove them by hand if you no longer want them.")
    if files:
        lines += ["Settings and other files it would put back as they were:", *files]
    if database is not None:
        imported, reimported, removed = _documents(database, backup / "hearth.sqlite")
        if imported:
            lines += ["Documents imported since, which it would remove from the knowledge base:", *(f"  {title}" for title in imported)]
        if reimported:
            lines += ["Documents re-imported since, which it would put back as they were:", *(f"  {title}" for title in reimported)]
        if removed:
            lines.append(f"{removed} document(s) removed since would come back.")
        if imported or reimported or removed:
            lines.append("The semantic index would then not match the documents until it is rebuilt.\n"
                         "Next, afterwards: hearth health, then rebuild the index from the Knowledge tab")
    return lines or ["Nothing has changed in your data since that backup."]


def _documents(live: Path, copy: Path) -> tuple[list[str], list[str], int]:
    """Documents only in `live` and those re-imported since, by name, and how many are only in `copy`; both opened read-only."""
    held = []
    for path in (live, copy):
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            held.append({path: (name, fingerprint) for path, name, fingerprint in
                         connection.execute("SELECT canonical_path, display_name, source_fingerprint FROM documents")})
        finally:
            connection.close()
    now, then = held
    return (sorted(now[path][0] for path in now.keys() - then.keys()),
            sorted(now[path][0] for path in now.keys() & then.keys() if now[path][1] != then[path][1]), len(then.keys() - now.keys()))


@contextlib.contextmanager
def only_update(home: Path) -> Iterator[None]:
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
    try:
        database = load_runtime_profile(path).database
    except RuntimeProfileError as exc:
        raise InstallError(f"{path}: {exc}\nNext: fix {path}, then run the update again.") from None
    return database if database is not None and database.is_file() else None


def _backup(home: Path, database: Path | None, ref: str, current: str | None, kind: str) -> Path:
    """A consistent copy of the database and Hearth's home, put in place only once complete; links are kept, never followed."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")  # Unique, even for two updates a second apart.
    final = home / BACKUPS / f"{stamp}-{''.join(c if c.isalnum() or c in '.-_' else '-' for c in ref)}"
    partial = home / BACKUPS / f".partial-{final.name}"
    partial.mkdir(parents=True)
    shutil.copytree(home, partial / "home", symlinks=True, ignore=lambda folder, names: [name for name in names if _skipped(Path(folder) / name, database)])
    if database is not None:
        source, copy = sqlite3.connect(database), sqlite3.connect(partial / "hearth.sqlite")
        try:
            source.backup(copy)  # SQLite's own consistent copy, never a copy of a file that may be mid-write.
        finally:
            source.close()
            copy.close()
    # What the home copy holds, made durable with the flag that it is complete, so a restore can prove nothing went missing.
    # Also the runtime that was current and why the backup was made, so a rollback knows what to go back to.
    (partial / "complete").write_text(json.dumps({"database": str(database) if database else None, "current": current, "kind": kind,
                                                  "home": _held(partial / "home")}), encoding="utf-8")
    os.sync()
    partial.rename(final)
    return final


def _skipped(path: Path, database: Path | None) -> bool:
    """Left out of the home copy and of its restore: SKIPPED names, and the database itself, which goes only through SQLite."""
    if path.name in SKIPPED:
        return True
    return database is not None and str(path) in {str(database), *(f"{database}{suffix}" for suffix in ("-journal", "-wal", "-shm"))}


def _restore(home: Path) -> None:
    """Put Hearth's home, the database, and the runtime back as the marker's backup holds them; safe to run again if interrupted."""
    marker = _marker(home)
    _restore_from(home, Path(marker.get("backup") or ""), Path(marker["database"]) if marker.get("database") else None,
                  Path(marker["root"]), marker.get("current_before"))
    _discard_marker(home, keep_backup=True)


def _restore_from(home: Path, backup: Path, database: Path | None, root: Path, runtime: str | None) -> None:
    """Make Hearth's home, the database, and `current` match `backup` and `runtime`, after checking the backup is whole."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)  # A second Ctrl+C must not cut a restore short.
    try:
        problem = _unusable(backup, database)
        if problem:
            raise InstallError(f"The update's backup at {backup} {problem}, so Hearth will not restore from it and has changed nothing.\n"
                               f"Next: keep {home} as it is and ask for help before running Hearth.")
        _sync(backup / "home", home, database)  # Settings and task records, as they were.
        if database is not None:
            # Through SQLite, not by copying the file: opening the live database first rolls back any journal a crash left.
            source, live = sqlite3.connect(f"{(backup / 'hearth.sqlite').as_uri()}?mode=ro", uri=True), sqlite3.connect(database)
            try:
                source.backup(live)
            finally:
                source.close()
                live.close()
        current = root / "current"
        if runtime and (not current.is_symlink() or os.readlink(current) != runtime):
            install.switch(current, root / runtime)
    finally:
        signal.signal(signal.SIGINT, previous)


def _unusable(backup: Path, database: Path | None) -> str | None:
    """Why a backup cannot be restored from, checked before anything is changed; a missing file is never opened, which
    would create an empty one and restore that."""
    if not (backup / "complete").is_file():
        return "is missing or incomplete"
    if not (backup / "home").is_dir():
        return "has no copy of Hearth's home"
    record = _complete(backup)
    if record is None:
        return "does not record what its copy of Hearth's home holds"
    recorded = record["home"]
    held = _held(backup / "home")
    for name, kind in recorded.items():
        if held.get(name) != kind:
            return f"is missing or has changed {name} in its copy of Hearth's home"
    if database is None:
        return None
    copy = backup / "hearth.sqlite"
    if not copy.is_file():
        return "has no copy of the database"
    try:
        connection = sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return "holds a damaged copy of the database"
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'documents'").fetchone() is None:
                return "holds a copy of the database without Hearth's tables"
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return "holds a copy of the database that SQLite cannot read"
    return None


def _complete(backup: Path) -> dict | None:
    """The backup's `complete` record, or None when it is missing or not the shape `_backup` writes."""
    try:
        record = json.loads((backup / "complete").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    shape = {"database": (str, type(None)), "current": (str, type(None)), "kind": (str,), "home": (dict,)}
    if not isinstance(record, dict) or any(not isinstance(record.get(key, ...), types) for key, types in shape.items()):
        return None
    if record["kind"] not in ("update", "before-rollback") or (record["current"] is not None and Path(record["current"]).name != record["current"]):
        return None  # `current` names a folder beside the others, never a path out of them.
    return record


def _held(folder: Path, database: Path | None = None) -> dict[str, list]:
    """Every entry under `folder` by relative path, as a folder, a link and its target, or a file and its size; what a
    backup leaves out is left out here too, so the live home and its copy compare."""
    held = {}
    for parent, folders, files in os.walk(folder):
        folders[:] = [name for name in folders if not _skipped(Path(parent, name), database)]
        for name in folders + [name for name in files if not _skipped(Path(parent, name), database)]:
            path = Path(parent, name)
            name = str(path.relative_to(folder))
            if path.is_symlink():
                held[name] = ["link", os.readlink(path)]
            else:
                held[name] = ["folder"] if path.is_dir() else ["file", path.stat().st_size]
    return held


def _sync(source: Path, live: Path, database: Path | None) -> None:
    """Make `live` match `source`, Hearth's home as it was backed up, leaving alone what the backup never held: the locks,
    backups, app data, worktrees, Codex homes, and the database. Links are copied as links. Safe to run again."""
    for entry in live.iterdir():
        if not _skipped(entry, database) and not (source / entry.name).exists() and not (source / entry.name).is_symlink():
            _remove(entry, database)  # Made since the backup.
    for entry in source.iterdir():
        target = live / entry.name
        if _skipped(target, database):
            continue
        if entry.is_dir() and not entry.is_symlink():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                target.unlink()
            target.mkdir(exist_ok=True)
            _sync(entry, target, database)
            continue
        if target.is_dir() and not target.is_symlink():
            _remove(target, database)
        elif target.exists() or target.is_symlink():
            target.unlink()
        shutil.copy2(entry, target, follow_symlinks=False)


def _remove(path: Path, database: Path | None) -> None:
    """Remove what a backup holds at `path`, keeping what it never holds, such as a task's Codex home and its sessions,
    which no restore could bring back; a folder that keeps any of it stays."""
    if not path.is_dir() or path.is_symlink():
        path.unlink()
        return
    for entry in path.iterdir():
        if not _skipped(entry, database):
            _remove(entry, database)
    if not any(path.iterdir()):
        path.rmdir()


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
        _stop(backend)  # Reaped here, before any restore or release: it holds the lock's descriptor until it has exited.


def _stop(process: subprocess.Popen) -> None:
    """Stop a process and reap it; one that ignores SIGTERM is killed, since Popen.communicate's timeout alone kills nothing."""
    process.terminate()
    try:
        process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def _continue_refused(runtime: Path) -> tuple[list[str], list[str]]:
    """After the update, continue each dashboard answer or approval it refused, through the new runtime (ADR-0040);
    returns the tasks continued and those that still need `hearth task resume`."""
    continued, failed = [], []
    for record in sorted(hearth_home().glob("tasks/*/resume.json")):
        pid = json.loads(record.read_text(encoding="utf-8")).get("pid")
        if _alive(pid) or not (record.parent / f"refused-{pid}.json").exists():
            continue  # Not a continuation an update refused.
        result = subprocess.run([str(runtime / "bin/python"), "-I", "-m", "hearth.cli", "task", "resume", record.parent.name],
                                capture_output=True, text=True, env=clean_environment(), cwd=Path.home(), timeout=120)
        (continued if result.returncode == 0 else failed).append(record.parent.name)
    return continued, failed


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
