from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearth.cli import main


class MvpWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        home = mock.patch.dict(os.environ, {"HOME": str(self.root)})  # Writers take the maintenance lock under ~/.hearth.
        home.start()
        self.addCleanup(home.stop)
        self.database = self.root / "hearth.sqlite"
        fixture = Path(__file__).parent / "fixtures" / "public" / "evaluation-note.md"
        self.note = self.root / fixture.name
        self.note.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_personal_collection_workflow_imports_checks_searches_reindexes_and_removes(self) -> None:
        imported = self._run("import", str(self.note))
        health = self._run("health")
        answer = self._run("search", "Where is the archive location?")
        inspection = self._run("inspect", "1")

        self.assertIn("Imported document", imported)
        self.assertIn("Extracted: 1 pages, 1 chunks, 0 OCR pages.", imported)
        self.assertIn("Documents: 1", health)
        self.assertIn("Source files unavailable: 0", health)
        self.assertIn("Answer\n# Operations", answer)
        self.assertIn("The archive location is local encrypted storage.", answer)
        self.assertIn("Sources\n- evaluation-note.md, page 1, section Operations", answer)
        self.assertIn("Document 1: evaluation-note.md", inspection)
        self.note.write_text("# Operations\n\nThe archive location is the south vault.\n", encoding="utf-8")
        reindexed = self._run("reindex", str(self.note))
        updated_answer = self._run("search", "Where is the archive location?")
        removed = self._run("remove", str(self.note))
        empty_health = self._run("health")
        self.assertIn("Reindexed document", reindexed)
        self.assertIn("Answer\n# Operations", updated_answer)
        self.assertIn("The archive location is the south vault.", updated_answer)
        self.assertIn("Sources\n- evaluation-note.md, page 1, section Operations", updated_answer)
        self.assertEqual(removed, "Removed.\n")
        self.assertIn("Documents: 0", empty_health)
        self.assertIn("Next: import a document, or search the current collection.", empty_health)

    def test_health_flags_source_change_until_explicit_reindex(self) -> None:
        self._run("import", str(self.note))
        self.note.write_text("# Operations\n\nThe deployment owner is Lin.\n", encoding="utf-8")

        changed_health = self._run("health")

        self.assertIn("Sources changed since import: 1", changed_health)
        self.assertIn("Document 1: evaluation-note.md - source changed since import", changed_health)
        self.assertIn("Next: reindex the source file when you are ready to refresh its extracted content.", changed_health)
        self.assertNotIn("The deployment owner is Lin.", changed_health)
        self._run("reindex", str(self.note))

        refreshed_health = self._run("health")

        self.assertIn("Sources changed since import: 0", refreshed_health)
        self.assertNotIn("Needs attention", refreshed_health)

    def test_remove_can_clear_an_unavailable_source_record(self) -> None:
        self._run("import", str(self.note))
        self.note.unlink()

        removed = self._run("remove", str(self.note))

        self.assertEqual(removed, "Removed.\n")
        self.assertIn("Documents: 0", self._run("health"))

    def _run(self, *command: str) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), *command])
        self.assertEqual(exit_code, 0)
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()
