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

    def test_relink_preserves_citations_for_an_externally_moved_matching_source(self) -> None:
        self._run("import", str(self.note))
        replacement = self.root / "relocated-note.md"
        self.note.rename(replacement)

        unavailable_health = self._run("health")
        preview = self._run("relink", "preview", "1", str(replacement))

        self.assertIn("Source files unavailable: 1", unavailable_health)
        self.assertIn("Preview: relink document 1: evaluation-note.md", preview)
        self.assertIn(f"Previous source (unavailable): {self.note.resolve()}", preview)
        self.assertIn(f"Replacement source: {replacement.resolve()}", preview)
        self.assertIn("No changes made.", preview)
        self.assertTrue(replacement.is_file())

        applied = self._run("relink", "apply", "1", str(replacement))
        relinked_health = self._run("health")
        answer = self._run("search", "Where is the archive location?")

        self.assertIn("Applied: relink document 1: evaluation-note.md -> relocated-note.md", applied)
        self.assertIn("Existing extracted text, citation chunk IDs, and semantic index were preserved.", applied)
        self.assertIn("Source files unavailable: 0", relinked_health)
        self.assertIn("Sources\n- relocated-note.md, page 1, section Operations", answer)

    def test_relink_refuses_a_replacement_with_different_contents(self) -> None:
        self._run("import", str(self.note))
        replacement = self.root / "different-note.md"
        replacement.write_text("# Different\n\nThis is not the imported document.\n", encoding="utf-8")
        self.note.unlink()
        error = io.StringIO()

        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as exit_context:
            main(["--database", str(self.database), "relink", "apply", "1", str(replacement)])

        self.assertEqual(exit_context.exception.code, 2)
        self.assertIn("does not match the imported source fingerprint", error.getvalue())
        self.assertIn("Source files unavailable: 1", self._run("health"))
        self.assertTrue(replacement.is_file())

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
