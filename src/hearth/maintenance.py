"""ADR-0038's maintenance lock: an update holds it exclusively, and every writer holds it shared while it runs."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

# Commands that never change Hearth's data. Everything else counts as a writer, so a new command is guarded until it is
# listed here. Knowledge commands, `hearth web` included, write: opening the database creates or migrates its schema.
# The statusline is listed because it skips its usage sample during an update rather than fail.
READ_ONLY = frozenset({"stats", "open", "usage", "usage statusline", "task list", "task show", "task open", "google status", "tools"})
REFUSAL = "Hearth is being updated, and this command changes its data."


class Held(Exception):
    """Raised where a writer would start while an update holds the maintenance lock."""


def writes(command: str) -> bool:
    return command not in READ_ONLY


@contextlib.contextmanager
def writing(home: Path) -> Iterator[None]:
    """Hold the lock shared for as long as a writer runs, so an update can neither start under it nor be started under."""
    interrupted = home / "update.json"
    if interrupted.exists():  # An update died after changing data may have begun: nothing writes until it is recovered.
        try:
            source = json.loads(interrupted.read_text(encoding="utf-8")).get("source")
        except (OSError, ValueError):
            source = None
        where = f"cd {json.dumps(source)} && " if source else "in the Hearth checkout, run "
        raise Held(f"A Hearth update was interrupted, and this command changes its data.\nNext: {where}.venv/bin/python -m hearth.install recover")
    path = home / "maintenance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:  # Not inherited by child processes, so an agent never holds an update off.
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Held(REFUSAL) from None
        yield


class NotHeld(Exception):
    """Raised for an inherited descriptor that is not the maintenance lock, held exclusively, as an update holds it."""


def adopt(fd: int, home: Path) -> None:
    """ADR-0040: run inside an update under the updater's own lock, inherited as `fd`, instead of taking a shared one.

    The descriptor must be the very file at <home>/maintenance.lock and must itself own its exclusive lock. Ownership is
    proven by asking for that same exclusive lock on `fd` without waiting: with another holder it fails, and on the owner
    it changes nothing, since the lock's type stays the same. Nothing here ever unlocks `fd` or asks for a shared lock on
    it: a lock belongs to the open file description, so that would release or weaken the updater's lock too. It is then
    made close-on-exec, so nothing this process starts, agents included, gets it.
    """
    path = home / "maintenance.lock"
    try:
        given, expected = os.fstat(fd), os.stat(path)
    except OSError:
        raise NotHeld(f"Descriptor {fd} is not Hearth's maintenance lock at {path}.") from None
    if not stat.S_ISREG(given.st_mode) or (given.st_dev, given.st_ino) != (expected.st_dev, expected.st_ino):
        raise NotHeld(f"Descriptor {fd} is not Hearth's maintenance lock at {path}.")
    with path.open("a") as probe:  # Another open file description: first, someone must hold the lock exclusively.
        try:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise NotHeld(f"{path} is not held exclusively, so no update is running to work inside.")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Succeeds only where this descriptor is that holder.
    except BlockingIOError:
        raise NotHeld(f"Descriptor {fd} does not hold the update's lock on {path}; another process does.") from None
    os.set_inheritable(fd, False)
