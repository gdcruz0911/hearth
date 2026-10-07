"""ADR-0036: the record of every vault file Hearth wrote for an agent, and the recall filter it decides.

Each file Hearth writes, a promoted report or an agent note, is recorded in `~/.hearth/vault-notes.jsonl` before it
is written, then marked written and committed. The record, not the folder or Git, says who wrote a file, so origin
survives a move or an edit. Recall consults it for every excerpt from `agent-notes/`, `reports/`, and `notes/`, and
missing information refuses an excerpt rather than treating it as unrestricted.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

LEDGER = "vault-notes.jsonl"
FOLDERS = ("agent-notes", "reports", "notes")
NOTE_ID = re.compile(r"^hearth-note:\s*(\S+)\s*$", re.M)


def vault(home: Path) -> Path:
    policy = home / "recall.json"
    return Path(json.loads(policy.read_text(encoding="utf-8")).get("vault", "~/Hearth") if policy.exists() else "~/Hearth").expanduser()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def note_id(*parts: str) -> str:
    """The same task, run, and title always give the same ID, so a retry finds its own earlier record."""
    return digest("\0".join(parts))[:16]


def record(home: Path, entry: dict) -> None:
    """Append one state of one file; the last line for an ID is its current state."""
    with (home / LEDGER).open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(entry) + "\n")


def load(home: Path) -> dict[str, dict]:
    """Current entries by note ID; raises ValueError when a line cannot be read, because a gap could hide a restriction."""
    path = home / LEDGER
    if not path.exists():
        return {}
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError("a vault note record line has no ID")
        entries[entry["id"]] = entry
    return entries


def front_matter_id(text: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    match = NOTE_ID.search(text[:end] if end > 0 else "")
    return match.group(1) if match else None


def locate(home: Path, root: Path) -> dict | None:
    """Where each recorded file is now, found by note ID, then path, then hash; None when the record cannot be read."""
    try:
        entries = load(home)
    except (OSError, ValueError):
        return None
    files = {}
    for folder in FOLDERS:
        for path in sorted((root / folder).rglob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            files[path.resolve()] = (front_matter_id(text), digest(text))
    by_id = {found: path for path, (found, _) in files.items() if found}
    by_hash = {hashed: path for path, (_, hashed) in files.items()}
    owner, lost = {}, []
    for key, entry in entries.items():
        recorded = Path(entry.get("path", "")).resolve()
        path = by_id.get(key) or (recorded if recorded in files else None) or by_hash.get(entry.get("sha256"))
        if path is not None:
            owner[path] = key
        elif entry.get("state") != "intended":  # An intended file that was never written has nothing to lose.
            lost.append(entry)
    return {"entries": entries, "owner": owner, "lost": lost}


def covers(scope: object, roots: tuple[Path, ...]) -> bool:
    """ADR-0029's rule: every folder the task's evidence was permitted from lies inside one of `roots`."""
    return isinstance(scope, list) and all(any(Path(folder).is_relative_to(root) for root in roots) for folder in scope)


def allows(state: dict | None, root: Path, path: Path, roots: tuple[Path, ...]) -> tuple[bool, str | None]:
    """Whether an excerpt from `path` may go to a provider with `roots`, with its origin label or the reason it may not."""
    path = path.resolve()
    folder = path.relative_to(root.resolve()).parts[0] if path.is_relative_to(root.resolve()) else None
    if folder not in FOLDERS:
        return True, None
    if state is None:
        return False, "the vault note record cannot be read"
    key = state["owner"].get(path)
    if key is None:
        if folder != "notes":
            return False, f"no record of who wrote this file in {folder}/"
        # ponytail: one lost note withholds all of notes/ from an uncovered provider; per-note tracking would need a stable ID.
        if any(not covers(entry.get("scope"), roots) for entry in state["lost"]):
            return False, "a recorded note's origin is lost and this provider does not cover its scope"
        return True, None
    entry = state["entries"][key]
    if folder == "agent-notes":
        return False, "an agent note the keeper has not accepted"
    if not covers(entry.get("scope"), roots):
        return False, "this provider does not cover the scope of the task that wrote it"
    return True, f"written by {entry.get('provider')} in task {entry.get('task')}, accepted by the keeper"
