from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from hearth.workbench import instructions

LOCAL = ["AGENTS.md", "CLAUDE.md", "CODING_REQUIREMENTS.md", "CONTEXT.md", "VERIFY.md", "docs/standards"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class CaptureTests(unittest.TestCase):
    """AGENTS.md and its standalone @imports, from approved sources only, as every worker receives them."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repo = Path(self.temporary_directory.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / ".gitignore").write_text("docs/standards/\n", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("# Agent rules\n@docs/standards/design.md\nRun the check.\n", encoding="utf-8")
        (self.repo / "CLAUDE.md").write_text("# Claude only\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "add", "-f", "AGENTS.md", "CLAUDE.md")  # Tracked even where a personal global ignore file lists them.
        self.commit("init")
        (self.repo / "docs/standards").mkdir(parents=True)
        (self.repo / "docs/standards/design.md").write_text("Design rules.\n", encoding="utf-8")

    def commit(self, message: str) -> None:
        git(self.repo, "-c", "user.email=p@e", "-c", "user.name=P", "commit", "-q", "-m", message)
        self.base = git(self.repo, "rev-parse", "HEAD").strip()

    def untrack_agents(self) -> None:
        git(self.repo, "rm", "-q", "--cached", "AGENTS.md")
        self.commit("AGENTS.md is local guidance")

    def capture(self) -> tuple[str, list[dict]]:
        return instructions.capture(self.repo, self.base, LOCAL)

    def test_agents_md_comes_from_the_base_commit_and_its_import_from_local_guidance(self) -> None:
        (self.repo / "AGENTS.md").write_text("Edited after the base commit.\n", encoding="utf-8")  # Uncommitted: not delivered.

        text, files = self.capture()

        self.assertEqual(text, "# Agent rules\nDesign rules.\nRun the check.\n")
        self.assertEqual([(item["path"], item["origin"]) for item in files], [("AGENTS.md", "base"), ("docs/standards/design.md", "local")])
        self.assertEqual(files[1]["sha256"], hashlib.sha256(b"Design rules.\n").hexdigest())

    def test_a_claude_md_beside_it_is_not_read(self) -> None:
        text, files = self.capture()

        self.assertNotIn("Claude only", text)
        self.assertNotIn("CLAUDE.md", [item["path"] for item in files])

    def test_without_agents_md_nothing_is_delivered(self) -> None:
        git(self.repo, "rm", "-q", "AGENTS.md")
        self.commit("no AGENTS.md")

        self.assertEqual(self.capture(), ("", []))

    def test_an_untracked_agents_md_counts_as_configured_local_guidance(self) -> None:
        self.untrack_agents()

        _, files = self.capture()

        self.assertEqual((files[0]["path"], files[0]["origin"]), ("AGENTS.md", "local"))

    def test_imports_are_deduplicated_and_only_standalone_lines_import(self) -> None:
        (self.repo / "docs/standards/agents.md").write_text("@design.md\nSee @not-an-import here.\n@design.md\n", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("@docs/standards/agents.md\n", encoding="utf-8")
        git(self.repo, "add", "-f", "AGENTS.md")
        self.commit("import")

        text, files = self.capture()

        self.assertEqual(text, "Design rules.\nSee @not-an-import here.\n")
        self.assertEqual([item["path"] for item in files], ["AGENTS.md", "docs/standards/agents.md", "docs/standards/design.md"])

    def test_imports_that_cannot_be_delivered_are_refused_with_the_reason(self) -> None:
        self.untrack_agents()
        (self.repo / "notes.md").write_text("Personal notes, untracked and not configured guidance.\n", encoding="utf-8")
        cases = {"@missing.md\n": "missing.md", "@../outside.md\n": "outside", "@/etc/hosts\n": "outside",
                 "@~/.codex/AGENTS.md\n": "outside", "@AGENTS.md\n": "cycle", "@notes.md\n": "not approved"}
        for agents, reason in cases.items():
            with self.subTest(agents=agents):
                (self.repo / "AGENTS.md").write_text(agents, encoding="utf-8")
                with self.assertRaises(instructions.InstructionsError) as refused:
                    self.capture()
                self.assertIn(reason, str(refused.exception))
                self.assertIn("Next:", str(refused.exception))

    def test_a_local_import_that_resolves_outside_the_repository_is_refused(self) -> None:
        self.untrack_agents()
        personal = Path(self.temporary_directory.name) / "personal"
        personal.mkdir()
        (personal / "notes.md").write_text("PERSONAL-MARKER\n", encoding="utf-8")
        (self.repo / "docs/standards/linked.md").symlink_to(personal / "notes.md")  # A symlinked file.
        (self.repo / "docs/standards/folder").symlink_to(personal)  # A symlinked parent folder.
        for target in ("docs/standards/linked.md", "docs/standards/folder/notes.md"):
            with self.subTest(target=target):
                (self.repo / "AGENTS.md").write_text(f"@{target}\n", encoding="utf-8")
                with self.assertRaises(instructions.InstructionsError) as refused:
                    self.capture()
                self.assertIn("outside the project", str(refused.exception))

    def test_resolution_is_bounded(self) -> None:
        self.untrack_agents()
        (self.repo / "AGENTS.md").write_text("@docs/standards/a0.md\n", encoding="utf-8")
        for number in range(instructions.MAX_DEPTH + 2):
            (self.repo / f"docs/standards/a{number}.md").write_text(f"@a{number + 1}.md\n", encoding="utf-8")

        with self.assertRaises(instructions.InstructionsError) as refused:
            self.capture()

        self.assertIn("deeper than", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
