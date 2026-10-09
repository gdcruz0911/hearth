"""Project instructions Hearth delivers to every worker itself, instead of letting each provider's CLI discover them.

`AGENTS.md` is the one instruction root for every provider; a `CLAUDE.md` beside it is not read. Hearth captures it, with
the files it imports through a standalone `@path` line, when a task starts, and delivers that copy in every worker's
prompt, so neither a later edit nor a provider's own discovery can replace it. A tracked file comes from the task's base
commit, an untracked one only from the project's configured local guidance, and nothing outside the repository is followed.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import subprocess
from pathlib import Path

ROOT = "AGENTS.md"
MAX_DEPTH = 5
MAX_FILES = 20
MAX_BYTES = 200_000
IMPORT = re.compile(r"^@(\S+)\s*$")


class InstructionsError(ValueError):
    """Raised for project instructions Hearth cannot deliver exactly."""


def capture(repo: Path, base: str, local: list[str]) -> tuple[str, list[dict]]:
    """The delivered text, and each file in it as {path, origin, sha256}; origin is "base" or "local"."""
    if _source(repo, base, local, ROOT) is None:
        return "", []
    files: list[dict] = []
    text = _expand(repo, base, local, ROOT, [], files)
    if len(text.encode("utf-8")) > MAX_BYTES:
        _refuse(f"the project instructions exceed {MAX_BYTES} bytes")
    return text, files


def _expand(repo: Path, base: str, local: list[str], path: str, chain: list[str], files: list[dict]) -> str:
    if path in chain:
        _refuse(f"{' imports '.join(chain + [path])} is a cycle")
    if len(chain) > MAX_DEPTH:
        _refuse(f"imports from {chain[0]} go deeper than {MAX_DEPTH} levels")
    found = _source(repo, base, local, path)
    if found is None:
        _refuse(f"{chain[-1] if chain else 'the project'} imports {path}, which is missing")
    content, origin = found
    if any(item["path"] == path for item in files):
        return ""  # Delivered once already.
    if len(files) >= MAX_FILES:
        _refuse(f"the project instructions import more than {MAX_FILES} files")
    files.append({"path": path, "origin": origin, "sha256": hashlib.sha256(content).hexdigest()})
    lines = []
    for line in content.decode("utf-8").splitlines(keepends=True):
        imported = IMPORT.match(line.strip())
        if imported is None:
            lines.append(line)
            continue
        target = imported.group(1)
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        if target.startswith(("/", "~")) or resolved == ".." or resolved.startswith("../"):
            _refuse(f"{path} imports {target}, which is outside the project")
        if _source(repo, base, local, resolved) is None and (repo / resolved).exists() and not _tracked(repo, base, resolved):
            _refuse(f"{path} imports {resolved}, which is neither tracked nor configured local guidance, so it is not approved")
        lines.append(_expand(repo, base, local, resolved, chain + [path], files))
    return "".join(lines)


def _source(repo: Path, base: str, local: list[str], path: str) -> tuple[bytes, str] | None:
    if _tracked(repo, base, path):
        return subprocess.run(["git", "-C", str(repo), "show", f"{base}:{path}"], check=True, capture_output=True).stdout, "base"
    approved = any(path == entry or path.startswith(entry.rstrip("/") + "/") for entry in local)
    if approved and (repo / path).is_file():
        if not (repo / path).resolve().is_relative_to(repo.resolve()):  # A symlinked file or parent folder may point anywhere.
            _refuse(f"{path} resolves outside the project")
        return (repo / path).read_bytes(), "local"
    return None


def _tracked(repo: Path, base: str, path: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{base}:{path}"], capture_output=True).returncode == 0


def _refuse(problem: str) -> None:
    raise InstructionsError(f"Project instructions: {problem}.\nNext: fix the import in the project's AGENTS.md, "
                            "or add the file to the repository")
