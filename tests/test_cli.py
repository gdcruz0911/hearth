from __future__ import annotations

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from hearth.cli import main
from hearth.domain import ExtractedPage, SourceDocument
from hearth.service import HearthService


class OcrPdfExtractor:
    def extract(self, path: Path) -> SourceDocument:
        return SourceDocument(
            path=path,
            pages=(
                ExtractedPage(
                    page_number=1,
                    text="OCR-derived archive metadata.",
                    extraction_method="ocr",
                    ocr_confidence=0.92,
                ),
            ),
        )


def _metadata_objects(value: object) -> list[dict]:
    if isinstance(value, dict):
        records = [value]
        for child in value.values():
            records.extend(_metadata_objects(child))
        return records
    if isinstance(value, list):
        records = []
        for child in value:
            records.extend(_metadata_objects(child))
        return records
    return []


class CollectionInspectionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "hearth.sqlite"
        self.note = self.root / "operations.md"
        self.note.write_text("# Operations\n\nThe deployment owner is Ada.\n", encoding="utf-8")
        service = HearthService(self.database)
        self.document_id = service.import_document(str(self.note))
        service.close()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_list_reports_metadata_without_document_text_or_source_path(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "list"])

        self.assertEqual(exit_code, 0)
        self.assertIn(f"{self.document_id}: operations.md", output.getvalue())
        self.assertIn("pages: 1, chunks: 1, OCR pages: 0", output.getvalue())
        self.assertIn("Next: inspect <document-id>, or search.", output.getvalue())
        self.assertNotIn("The deployment owner is Ada.", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_list_empty_collection_explains_how_to_start(self) -> None:
        empty_database = self.root / "empty.sqlite"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(empty_database), "list"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), "No imported documents.\nNext: import <local-file>, then run health or search.\n")

    def test_health_reports_collection_metadata_and_next_action_without_document_text(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "health"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Collection health", output.getvalue())
        self.assertIn("Documents: 1", output.getvalue())
        self.assertIn("Pages: 1", output.getvalue())
        self.assertIn("Chunks: 1", output.getvalue())
        self.assertIn("OCR pages needing review: 0", output.getvalue())
        self.assertIn("Source files unavailable: 0", output.getvalue())
        self.assertIn("Sources changed since import: 0", output.getvalue())
        self.assertIn("Sources requiring baseline reindex: 0", output.getvalue())
        self.assertIn("Semantic index: not configured", output.getvalue())
        self.assertIn("Next: import a document, or search the current collection.", output.getvalue())
        self.assertNotIn("The deployment owner is Ada.", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_health_reports_unavailable_source_without_rendering_its_path(self) -> None:
        self.note.rename(self.root / "operations-moved.md")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "health"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Source files unavailable: 1", output.getvalue())
        self.assertIn("Needs attention", output.getvalue())
        self.assertIn("Document 1: operations.md - source unavailable", output.getvalue())
        self.assertIn("remove its stale collection record and import the file from its new location", output.getvalue())
        self.assertNotIn("operations-moved.md", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_import_reports_extraction_and_derived_artifact_status(self) -> None:
        second_note = self.root / "security.md"
        second_note.write_text("# Security\n\nThe archive is local.\n", encoding="utf-8")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "import", str(second_note)])

        self.assertEqual(exit_code, 0)
        self.assertIn("Imported document", output.getvalue())
        self.assertIn("security.md", output.getvalue())
        self.assertIn("Extracted: 1 pages, 1 chunks, 0 OCR pages.", output.getvalue())
        self.assertIn("Semantic index: not configured.", output.getvalue())
        self.assertIn("OCR artifacts: not used.", output.getvalue())

    def test_profile_create_and_use_keep_runtime_paths_out_of_normal_list_output(self) -> None:
        profile_path = self.root / "runtime" / "hearth.json"
        create_output = io.StringIO()
        list_output = io.StringIO()

        with contextlib.redirect_stdout(create_output):
            create_exit_code = main(
                [
                    "profile",
                    "create",
                    str(profile_path),
                    "--database",
                    str(self.database),
                    "--relationship-minimum-score",
                    "0.81",
                ]
            )
        with contextlib.redirect_stdout(list_output):
            list_exit_code = main(["--profile", str(profile_path), "list"])

        self.assertEqual(create_exit_code, 0)
        self.assertEqual(list_exit_code, 0)
        self.assertTrue(profile_path.is_file())
        self.assertIn("Created private runtime profile", create_output.getvalue())
        self.assertIn(f"{self.document_id}: operations.md", list_output.getvalue())
        self.assertNotIn(str(self.root), list_output.getvalue())

    def test_sources_preview_and_import_use_an_explicit_connected_root(self) -> None:
        source_root = self.root / "Desktop"
        source_root.mkdir()
        source_note = source_root / "brief.md"
        source_note.write_text("# Brief\n\nThe delivery is on Friday.\n", encoding="utf-8")
        preview_output = io.StringIO()
        import_output = io.StringIO()

        with contextlib.redirect_stdout(preview_output):
            preview_exit_code = main(
                ["--database", str(self.database), "--source-root", str(source_root), "sources", "preview"]
            )
        with contextlib.redirect_stdout(import_output):
            import_exit_code = main(
                ["--database", str(self.database), "--source-root", str(source_root), "sources", "import"]
            )

        self.assertEqual(preview_exit_code, 0)
        self.assertEqual(import_exit_code, 0)
        self.assertIn("Desktop: 1 new supported files", preview_output.getvalue())
        self.assertIn("brief.md (Desktop)", preview_output.getvalue())
        self.assertIn("Imported: 1 files.", import_output.getvalue())
        self.assertNotIn(str(source_root), preview_output.getvalue())

    def test_search_uses_citation_first_output(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "search", "Who is the deployment owner?"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Answer\n# Operations", output.getvalue())
        self.assertIn("The deployment owner is Ada.", output.getvalue())
        self.assertIn("Sources\n- operations.md, page 1, section Operations", output.getvalue())
        self.assertIn("Evidence: # Operations", output.getvalue())
        self.assertIn("The deployment owner is Ada.", output.getvalue())

    def test_search_marks_ocr_citations_for_review(self) -> None:
        pdf = self.root / "scanned.pdf"
        pdf.write_bytes(b"placeholder")
        service = HearthService(self.database, pdf_extractor=OcrPdfExtractor())
        service.import_document(str(pdf))
        service.close()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "search", "What archive metadata is available?"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Sources", output.getvalue())
        self.assertIn("scanned.pdf, page 1", output.getvalue())
        self.assertIn("OCR warning: verify against the original document (confidence: 0.92).", output.getvalue())

    def test_inspect_reports_page_and_chunk_metadata_without_document_text(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "inspect", str(self.document_id)])

        service = HearthService(self.database)
        chunk = service.inspect_document(self.document_id).pages[0].chunks[0]
        service.close()
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue(),
            f"Document {self.document_id}: operations.md\n"
            "Page 1 (section: Operations, extraction: native, chunks: 1)\n"
            f"  Chunk {chunk.id} (characters: {chunk.char_start}-{chunk.char_end})\n",
        )
        self.assertNotIn("The deployment owner is Ada.", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_inspect_json_reports_document_page_and_chunk_provenance(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "inspect", str(self.document_id), "--json"])

        self.assertEqual(exit_code, 0)
        records = _metadata_objects(json.loads(output.getvalue()))
        self.assertTrue(any(record.get("id") == self.document_id and record.get("name") == "operations.md" for record in records))
        self.assertTrue(
            any(
                record.get("page_number") == 1
                and record.get("section") == "Operations"
                and record.get("extraction_method") == "native"
                for record in records
            )
        )
        self.assertTrue(
            any(
                isinstance(record.get("id"), int)
                and record.get("char_start") == 0
                and isinstance(record.get("char_end"), int)
                and record["char_end"] > 0
                for record in records
            )
        )
        self.assertNotIn("The deployment owner is Ada.", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_inspect_returns_failure_for_unknown_document(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "inspect", "999"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            output.getvalue(),
            "No imported document with ID 999.\nNext: run list to review imported documents and their IDs.\n",
        )

    def test_inspect_json_returns_one_json_value_for_unknown_document(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "inspect", "999", "--json"])

        self.assertEqual(exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload, {"error": "No imported document with ID 999."})

    def test_search_abstention_suggests_safe_next_actions(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "search", "What is the annual budget?"])

        self.assertEqual(exit_code, 0)
        self.assertIn("No evidence-bound answer was available from the imported documents.", output.getvalue())
        self.assertIn("Next: try different terms, run health for source attention, or import another local document.", output.getvalue())

    def test_help_includes_a_concise_common_workflow(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_context:
            main(["--help"])

        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn("Common workflow:", output.getvalue())
        self.assertIn("Find document IDs to inspect.", output.getvalue())

    def test_remove_missing_document_suggests_list(self) -> None:
        missing_path = self.root / "missing.md"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "remove", str(missing_path)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue(),
            "No matching document found.\nNext: run list to review imported documents and their IDs.\n",
        )

    def test_inspect_marks_ocr_pages_for_review_without_rendering_text(self) -> None:
        pdf = self.root / "scanned.pdf"
        pdf.write_bytes(b"placeholder")
        service = HearthService(self.database, pdf_extractor=OcrPdfExtractor())
        document_id = service.import_document(str(pdf))
        service.close()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "inspect", str(document_id)])

        self.assertEqual(exit_code, 0)
        self.assertIn("extraction: ocr", output.getvalue())
        self.assertIn("OCR confidence: 0.92", output.getvalue())
        self.assertIn("OCR warning: verify against the original document", output.getvalue())
        self.assertNotIn("OCR-derived archive metadata.", output.getvalue())

    def test_inspect_json_reports_ocr_provenance_without_document_text(self) -> None:
        pdf = self.root / "scanned.pdf"
        pdf.write_bytes(b"placeholder")
        service = HearthService(self.database, pdf_extractor=OcrPdfExtractor())
        document_id = service.import_document(str(pdf))
        service.close()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main(["--database", str(self.database), "inspect", str(document_id), "--json"])

        self.assertEqual(exit_code, 0)
        records = _metadata_objects(json.loads(output.getvalue()))
        self.assertTrue(any(record.get("id") == document_id and record.get("name") == "scanned.pdf" for record in records))
        self.assertTrue(
            any(
                record.get("page_number") == 1
                and record.get("extraction_method") == "ocr"
                and record.get("ocr_confidence") == 0.92
                for record in records
            )
        )
        self.assertNotIn("OCR-derived archive metadata.", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_retain_ocr_output_requires_an_output_directory(self) -> None:
        error = io.StringIO()

        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as exit_context:
            main(["--retain-ocr-output", "list"])

        self.assertEqual(exit_context.exception.code, 2)
        self.assertIn("--retain-ocr-output requires --ocr-output-directory.", error.getvalue())


if __name__ == "__main__":
    unittest.main()


class CommandReferenceTests(unittest.TestCase):
    """docs/commands.md lists every command and subcommand the parser accepts."""

    @staticmethod
    def subcommands(*argv: str) -> list[str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.suppress(SystemExit):
            main([*argv, "--help"])
        # Subcommands and choice positionals, not option choices such as --agent {a,b}.
        match = re.search(r"(?:\] |^\s+)\{([\w,-]+)\}", output.getvalue(), re.MULTILINE)
        return match.group(1).split(",") if match else []

    def test_the_command_list_names_every_command(self) -> None:
        readme = (Path(__file__).resolve().parents[1] / "docs/commands.md").read_text(encoding="utf-8")
        commands = []
        for command in self.subcommands():
            commands += [f"{command} {sub}" for sub in self.subcommands(command)] or [command]

        missing = [command for command in commands if f"`hearth {command}" not in readme]

        self.assertEqual(missing, [])
