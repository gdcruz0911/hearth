from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hearth.workbench import roles

GLOBAL = """# My roles

Prose around the tables is fine.

## review
| order | provider | model | effort |
|---|---|---|---|
| 2 | codex | gpt-6-sol | high |
| 1 | claude | claude-opus-5-5 | high |

## supervisor
| order | provider | model | effort |
|---|---|---|---|
| 1 | claude | claude-opus-5-5 | |
"""


class RolesFileTests(unittest.TestCase):
    """ADR-0039: the person's roles file, read strictly; Hearth never guesses a role."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.home = Path(self.temporary_directory.name)

    def write(self, text: str, name: str = "roles.md") -> None:
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_rows_are_read_in_their_order_with_empty_cells_left_to_the_defaults(self) -> None:
        self.write(GLOBAL)

        loaded = roles.load(self.home, "demo")

        self.assertEqual(loaded["review"], [{"provider": "claude", "model": "claude-opus-5-5", "effort": "high"},
                                            {"provider": "codex", "model": "gpt-6-sol", "effort": "high"}])
        self.assertEqual(loaded["supervisor"], [{"provider": "claude", "model": "claude-opus-5-5", "effort": None}])
        self.assertTrue(roles.exists(self.home))

    def test_no_file_means_no_roles(self) -> None:
        self.assertEqual(roles.load(self.home, "demo"), {})
        self.assertFalse(roles.exists(self.home))

    def test_a_project_file_replaces_only_the_roles_it_defines(self) -> None:
        self.write(GLOBAL)
        self.write("## review\n| order | provider | model | effort |\n|---|---|---|---|\n| 1 | codex | | |\n", "roles/demo.md")

        loaded = roles.load(self.home, "demo")

        self.assertEqual(loaded["review"], [{"provider": "codex", "model": None, "effort": None}])
        self.assertEqual(loaded["supervisor"][0]["provider"], "claude")
        self.assertEqual(roles.load(self.home, "other")["review"][0]["provider"], "claude")

    def test_malformed_files_are_refused_with_what_is_wrong(self) -> None:
        table = "| order | provider | model | effort |\n|---|---|---|---|\n"
        cases = {
            "an unknown role": "## reviewer\n" + table + "| 1 | claude | | |\n",
            "a missing column": "## review\n| order | provider | model |\n|---|---|---|\n| 1 | claude | |\n",
            "a row with too few cells": "## review\n" + table + "| 1 | claude |\n",
            "an order that is not a number": "## review\n" + table + "| first | claude | | |\n",
            "a repeated order": "## review\n" + table + "| 1 | claude | | |\n| 1 | codex | | |\n",
            "a role with no rows": "## review\n" + table,
            "a role given twice": "## review\n" + table + "| 1 | claude | | |\n## review\n" + table + "| 1 | codex | | |\n",
            "an empty provider": "## review\n" + table + "| 1 |  | | |\n",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.write(text)
                with self.assertRaises(roles.RolesError) as refused:
                    roles.load(self.home, "demo")
                self.assertIn("roles.md", str(refused.exception))
                self.assertIn("Next:", str(refused.exception))

    def test_a_project_file_cannot_set_the_supervisor(self) -> None:
        self.write("## supervisor\n| order | provider | model | effort |\n|---|---|---|---|\n| 1 | claude | | |\n", "roles/demo.md")

        with self.assertRaises(roles.RolesError):
            roles.load(self.home, "demo")


if __name__ == "__main__":
    unittest.main()
