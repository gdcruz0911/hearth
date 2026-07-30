from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from hearth.cli import main


class MvpWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
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
        renamed_note = self.root / "archive-notes.md"
        rename_preview = self._run("organize", "preview", "1", "--rename", renamed_note.name)

        self.assertIn("Imported document", imported)
        self.assertIn("Extracted: 1 pages, 1 chunks, 0 OCR pages.", imported)
        self.assertIn("Documents: 1", health)
        self.assertIn("Source files unavailable: 0", health)
        self.assertIn("Answer\n# Operations", answer)
        self.assertIn("The archive location is local encrypted storage.", answer)
        self.assertIn("Sources\n- evaluation-note.md, page 1, section Operations", answer)
        self.assertIn("Preview: rename document 1: evaluation-note.md", rename_preview)
        self.assertTrue(self.note.is_file())
        self.assertFalse(renamed_note.exists())
        rename_applied = self._run("organize", "apply", "1", "--rename", renamed_note.name)
        self.assertIn("Applied: rename document 1: evaluation-note.md -> archive-notes.md", rename_applied)
        self.assertFalse(self.note.exists())
        self.assertTrue(renamed_note.is_file())
        renamed_answer = self._run("search", "Where is the archive location?")
        self.assertIn("Sources\n- archive-notes.md, page 1, section Operations", renamed_answer)
        collection_folder = self.root / "organized"
        collection_folder.mkdir()
        organized_note = collection_folder / renamed_note.name
        move_preview = self._run("organize", "preview", "1", "--move-to", str(collection_folder))
        self.assertIn("Preview: move document 1: archive-notes.md", move_preview)
        self.assertTrue(renamed_note.is_file())
        self.assertFalse(organized_note.exists())
        move_applied = self._run("organize", "apply", "1", "--move-to", str(collection_folder))
        self.assertIn("Applied: move document 1: archive-notes.md -> archive-notes.md", move_applied)
        self.assertFalse(renamed_note.exists())
        self.assertTrue(organized_note.is_file())
        moved_inspection = self._run("inspect", "1")
        moved_health = self._run("health")
        self.assertIn("Document 1: archive-notes.md", moved_inspection)
        self.assertIn("Source files unavailable: 0", moved_health)
        organized_note.write_text("# Operations\n\nThe archive location is the south vault.\n", encoding="utf-8")
        reindexed = self._run("reindex", str(organized_note))
        updated_answer = self._run("search", "Where is the archive location?")
        removed = self._run("remove", str(organized_note))
        empty_health = self._run("health")
        self.assertIn("Reindexed document", reindexed)
        self.assertIn("Answer\n# Operations", updated_answer)
        self.assertIn("The archive location is the south vault.", updated_answer)
        self.assertIn("Sources\n- archive-notes.md, page 1, section Operations", updated_answer)
        self.assertEqual(removed, "Removed.\n")
        self.assertIn("Documents: 0", empty_health)
        self.assertIn("Next: import a document, or search the current collection.", empty_health)

    def test_organization_refuses_an_existing_target_without_moving_the_source(self) -> None:
        self._run("import", str(self.note))
        target = self.root / "occupied.md"
        target.write_text("Existing document.", encoding="utf-8")
        error = io.StringIO()

        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as exit_context:
            main(["--database", str(self.database), "organize", "apply", "1", "--rename", target.name])

        self.assertEqual(exit_context.exception.code, 2)
        self.assertIn("The target path already exists. Hearth will not overwrite files.", error.getvalue())
        self.assertTrue(self.note.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "Existing document.")

    def _run(self, *command: str) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), *command])
        self.assertEqual(exit_code, 0)
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()
