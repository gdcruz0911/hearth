"""ADR-0038's maintenance lock: an update holds it exclusively, and every writer holds it shared while it runs."""

from __future__ import annotations

import contextlib
import fcntl
from collections.abc import Iterator
from pathlib import Path

# Commands that never change Hearth's data. Everything else counts as a writer, so a new command is guarded until it is
# listed here. Knowledge commands, `hearth web` included, write: opening the database creates or migrates its schema.
# The statusline is listed because it skips its usage sample during an update rather than fail.
READ_ONLY = frozenset({"stats", "open", "usage", "usage statusline", "task list", "task show", "task open", "google status"})
REFUSAL = "Hearth is being updated, and this command changes its data."


class Held(Exception):
    """Raised where a writer would start while an update holds the maintenance lock."""


def writes(command: str) -> bool:
    return command not in READ_ONLY


@contextlib.contextmanager
def writing(home: Path) -> Iterator[None]:
    """Hold the lock shared for as long as a writer runs, so an update can neither start under it nor be started under."""
    path = home / "maintenance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:  # Not inherited by child processes, so an agent never holds an update off.
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Held(REFUSAL) from None
        yield
