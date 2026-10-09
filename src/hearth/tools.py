"""The command-line tools Hearth runs, found without the login shell's PATH (ADR-0038).

An app opened from Finder starts with only /usr/bin:/bin:/usr/sbin:/sbin, where none of the person's agents, Homebrew tools,
or tmux live. The desktop backend adds the folders a terminal on this Mac would search, in a terminal's order, so tasks, resumed loops,
and tmux sessions find the same tools they would from a terminal. `hearth tools` reports what was found there and what is missing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# The login shell's own order on 2026-10-09, without its Apple-internal cryptex folders.
TOOL_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin")

# name, what Hearth uses it for, whether Hearth cannot run tasks without it, and the argument that prints its version.
TOOLS = (
    ("git", "worktrees and checkpoints for every task", True, "--version"),
    ("claude", "Claude Code workers", False, "--version"),
    ("codex", "Codex workers", False, "--version"),
    ("agy", "Antigravity reviewers", False, "--version"),
    ("tmux", "watching and joining an agent's session", False, "-V"),
    ("gh", "pull requests and their checks", False, "--version"),
    ("pdftotext", "reading PDF text", False, "-v"),
    ("pdfinfo", "reading PDF page counts", False, "-v"),
    ("ocrmypdf", "reading scanned PDFs", False, "--version"),
    ("tesseract", "the text recognition ocrmypdf runs", False, "--version"),  # ocrmypdf --version passes without these two.
    ("gs", "the PDF rendering ocrmypdf runs (Ghostscript)", False, "--version"),
)
AGENTS = ("claude", "codex")  # Tasks need at least one of these to implement.
TIMEOUT_SECONDS = 15


def with_tool_dirs(path: str) -> str:
    """PATH with each tool folder this Mac has and PATH lacks, ahead of the rest, so Homebrew's tools win as in a terminal."""
    present = path.split(os.pathsep) if path else []
    added = [folder for folder in (os.path.expanduser(entry) for entry in TOOL_DIRS) if folder not in present and Path(folder).is_dir()]
    return os.pathsep.join(added + present)


def report(embedding_model: Path | None, reranker_model: Path | None) -> dict:
    """Each tool's path and version, or why it is missing, and whether local inference can run; nothing is downloaded."""
    tools = []
    search = with_tool_dirs(os.environ.get("PATH", ""))  # Where the app's backend looks, without changing this process.
    for name, purpose, required, version_flag in TOOLS:
        found = shutil.which(name, path=search)
        tools.append({"name": name, "purpose": purpose, "required": required, "path": found,
                      "version": _version([found, version_flag]) if found else None})
    found = {tool["name"] for tool in tools if tool["path"]}
    return {
        "tools": tools,
        "inference": _inference(embedding_model, reranker_model),
        "ready": all(tool["path"] for tool in tools if tool["required"]) and any(agent in found for agent in AGENTS),
    }


def _version(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (result.stdout or result.stderr).strip().splitlines()  # pdftotext and pdfinfo print their version to stderr.
    return lines[0] if lines else None


def _inference(embedding_model: Path | None, reranker_model: Path | None) -> dict:
    """Whether MLX imports, checked in its own process so a broken install cannot take Hearth down, and whether each model is on disk."""
    try:
        imported = subprocess.run([sys.executable, "-c", "import mlx.core, mlx_lm"], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "HF_HUB_OFFLINE": "1"}, stdin=subprocess.DEVNULL)
        error = None if imported.returncode == 0 else (imported.stderr.strip().splitlines() or ["MLX did not import"])[-1]
    except subprocess.TimeoutExpired:
        error = "importing MLX took longer than 60 seconds"
    models = {}
    for role, folder in (("embedding", embedding_model), ("reranker", reranker_model)):
        models[role] = None if folder is None else {"path": str(folder), "on_disk": folder.is_dir()}
    return {"mlx": error is None, "error": error, "models": models}
