from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hearth.domain import SourceRelinkError
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

    def test_relink_requires_a_fingerprint_for_a_legacy_source(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        database = Path(temporary_directory.name) / "hearth.sqlite"
        source = Path(temporary_directory.name) / "legacy.md"
        source.write_text("Legacy source.", encoding="utf-8")
        replacement = Path(temporary_directory.name) / "replacement.md"
        replacement.write_text("Legacy source.", encoding="utf-8")
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
        source.unlink()

        service = HearthService(database)
        self.addCleanup(service.close)

        with self.assertRaisesRegex(SourceRelinkError, "needs a baseline reindex"):
            service.plan_relink(1, replacement)


if __name__ == "__main__":
    unittest.main()
