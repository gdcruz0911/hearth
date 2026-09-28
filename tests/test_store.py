from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hearth.service import HearthService
from hearth.store import SQLiteStore


class SQLiteStoreMigrationTests(unittest.TestCase):
    def test_legacy_document_schema_adds_source_freshness_columns(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        database = Path(temporary_directory.name) / "hearth.sqlite"
        source = Path(temporary_directory.name) / "legacy.md"
        source.write_text("Legacy source.", encoding="utf-8")
        connection = sqlite3.connect(database)
        connection.execute(
            """CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            canonical_path TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO documents (canonical_path, display_name) VALUES (?, ?)", (str(source.resolve()), source.name)
        )
        connection.commit()
        connection.close()

        store = SQLiteStore(database)
        columns = {row["name"] for row in store._connection.execute("PRAGMA table_info(documents)")}
        record = store.source_records()[0]
        store.close()

        service = HearthService(database)
        self.addCleanup(service.close)
        health = service.collection_health()

        self.assertTrue({"source_fingerprint", "source_size", "source_mtime_ns"}.issubset(columns))
        self.assertEqual(record[3:], (None, None, None))
        self.assertEqual(health.baseline_reindex_count, 1)
        self.assertEqual(health.source_attention[0].status, "source needs baseline reindex")

        service.import_document(str(source))

        self.assertEqual(service.collection_health().baseline_reindex_count, 0)


class KeywordIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.directory = Path(temporary_directory.name)
        self.database = self.directory / "hearth.sqlite"
        self.decision = self.directory / "decision.md"
        self.decision.write_text("ADR-0024 keeps outbound data per destination.", encoding="utf-8")
        (self.directory / "other.md").write_text("The garden needs water on Tuesdays.", encoding="utf-8")

    def test_an_exact_identifier_is_found_and_forgotten_when_its_document_is_removed(self) -> None:
        service = HearthService(self.database)
        self.addCleanup(service.close)
        service.import_document(str(self.decision))
        service.import_document(str(self.directory / "other.md"))

        answer = service.answer("What does ADR-0024 decide?")

        self.assertEqual((answer.status, answer.citations[0].document_name), ("supported", "decision.md"))
        service.remove_document(str(self.decision))
        self.assertEqual(service.answer("What does ADR-0024 decide?").status, "abstained")

    def test_keyword_only_search_skips_the_semantic_index(self) -> None:
        from hearth.embedding import IndexCompatibilityError

        class StaleSemanticIndex:
            searched = 0

            def rebuild(self, chunks, **options) -> None:
                pass

            def search(self, question, chunks, limit=20):
                StaleSemanticIndex.searched += 1
                raise IndexCompatibilityError("The active local semantic index does not match.")

        service = HearthService(self.database, semantic_index=StaleSemanticIndex())
        self.addCleanup(service.close)
        service.import_document(str(self.decision))

        answer = service.answer("ADR-0024", keyword_only=True)

        self.assertEqual((answer.status, answer.citations[0].document_name), ("supported", "decision.md"))
        self.assertEqual(StaleSemanticIndex.searched, 0)
        with self.assertRaises(IndexCompatibilityError):
            service.answer("ADR-0024")

    def test_search_report_lists_a_retrieved_candidate_but_no_evidence_when_the_gate_abstains(self) -> None:
        from hearth.domain import Evidence

        class UnrelatedSemanticIndex:
            def rebuild(self, chunks, **options) -> None:
                pass

            def search(self, question, chunks, limit=20):
                return [Evidence(chunk=chunks[0], score=0.31)]

            def is_current(self, chunks) -> bool:
                return True

        service = HearthService(self.database, semantic_index=UnrelatedSemanticIndex())
        self.addCleanup(service.close)
        service.import_document(str(self.directory / "other.md"))

        report = service.search_report("Which zebra won?")

        self.assertEqual((report["status"], report["evidence"], report["gate"]["passed"]), ("abstained", [], False))
        self.assertEqual([(c["document"], c["cited"]) for c in report["candidates"]], [("other.md", False)])
        self.assertEqual(report["retrieval"]["mode"], "hybrid")

    def test_a_collection_imported_before_the_keyword_index_is_backfilled(self) -> None:
        service = HearthService(self.database)
        service.import_document(str(self.decision))
        service.close()
        connection = sqlite3.connect(self.database)
        connection.executescript(
            "DROP TRIGGER chunks_fts_insert; DROP TRIGGER chunks_fts_delete; DROP TABLE chunks_fts;"
        )
        connection.close()

        store = SQLiteStore(self.database)
        self.addCleanup(store.close)

        self.assertEqual(len(store.keyword_search({"0024"})), 1)


if __name__ == "__main__":
    unittest.main()
