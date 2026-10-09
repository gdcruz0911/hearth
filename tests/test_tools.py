from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearth import tools
from hearth.cli import main

BARE = "/usr/bin:/bin:/usr/sbin:/sbin"  # What an app opened from Finder starts with.


class ToolDiscoveryTests(unittest.TestCase):
    """ADR-0038: the app finds the person's tools without the login shell's PATH, and says plainly what is missing."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.first, self.second = self.root / "local-bin", self.root / "homebrew-bin"
        self.first.mkdir()
        self.second.mkdir()
        patch = mock.patch.object(tools, "TOOL_DIRS", (str(self.first), str(self.root / "absent"), str(self.second)))
        patch.start()
        self.addCleanup(patch.stop)

    def tool(self, folder: Path, name: str, version: str) -> None:
        script = folder / name
        script.write_text(f"#!/bin/sh\necho '{version}'\n", encoding="utf-8")
        script.chmod(0o755)

    def run_tools(self, *argv: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"PATH": BARE, "HEARTH_HOME": str(self.root / "hearth")}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main(["tools", *argv])
            path = os.environ["PATH"]
        return status, output.getvalue(), errors.getvalue() + f"\nPATH after={path}"

    def test_the_tool_folders_go_first_in_order_and_only_when_they_exist_and_are_missing(self) -> None:
        self.assertEqual(tools.with_tool_dirs(BARE), f"{self.first}:{self.second}:{BARE}")
        self.assertEqual(tools.with_tool_dirs(f"{self.second}:{BARE}"), f"{self.first}:{self.second}:{BARE}")
        self.assertEqual(tools.with_tool_dirs(""), f"{self.first}:{self.second}")

    def test_tools_in_those_folders_are_found_from_a_bare_path_with_their_versions(self) -> None:
        self.tool(self.first, "claude", "2.1.0 (Claude Code)")
        self.tool(self.second, "git", "git version 9.9")  # Ahead of /usr/bin/git, as in a terminal.

        status, output, errors = self.run_tools("--json")

        found = {tool["name"]: tool for tool in json.loads(output)["tools"]}
        self.assertEqual(status, 0)
        self.assertEqual((found["claude"]["path"], found["claude"]["version"]), (str(self.first / "claude"), "2.1.0 (Claude Code)"))
        self.assertEqual(found["git"]["path"], str(self.second / "git"))
        self.assertIn(f"PATH after={BARE}", errors)  # Only the desktop backend widens its own PATH.

    def test_without_an_agent_that_implements_it_says_what_is_missing_and_fails(self) -> None:
        self.tool(self.second, "git", "git version 9.9")

        status, output, errors = self.run_tools()

        self.assertEqual(status, 1)
        self.assertRegex(output, r"(?m)^claude +Claude Code workers: missing$")
        self.assertIn("Missing: claude, codex", errors)
        self.assertIn("Next:", errors)

    def test_model_folders_are_reported_without_loading_anything(self) -> None:
        on_disk = self.root / "embedding"
        on_disk.mkdir()

        found = tools.report(on_disk, self.root / "reranker")["inference"]

        self.assertEqual(found["models"], {"embedding": {"path": str(on_disk), "on_disk": True},
                                           "reranker": {"path": str(self.root / "reranker"), "on_disk": False}})
        self.assertIsInstance(found["mlx"], bool)

    def test_a_broken_inference_install_is_reported_not_raised(self) -> None:
        with mock.patch.object(tools.subprocess, "run", return_value=mock.Mock(returncode=1, stderr="ModuleNotFoundError: No module named 'mlx'")):
            self.assertEqual(tools._inference(None, None)["error"], "ModuleNotFoundError: No module named 'mlx'")


if __name__ == "__main__":
    unittest.main()
