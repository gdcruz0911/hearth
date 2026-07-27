from __future__ import annotations

import sqlite3
from pathlib import Path

from .domain import Chunk, ExtractedPage


class SQLiteStore:
    """Authoritative local metadata store. Retrieval indexes are derived data."""

    def __init__(self, database_path: Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

    def replace_document(self, path: Path, pages: tuple[ExtractedPage, ...], chunks_by_page: dict[int, list[tuple[str, int, int]]]) -> int:
        with self._connection:
            self._connection.execute("DELETE FROM documents WHERE canonical_path = ?", (str(path),))
            document_id = self._connection.execute(
                "INSERT INTO documents (canonical_path, display_name) VALUES (?, ?)", (str(path), path.name)
            ).lastrowid
            for page in pages:
                page_id = self._connection.execute(
                    """INSERT INTO pages (document_id, page_number, section, extraction_method, ocr_confidence)
                    VALUES (?, ?, ?, ?, ?)""",
                    (document_id, page.page_number, page.section, page.extraction_method, page.ocr_confidence),
                ).lastrowid
                self._connection.executemany(
                    """INSERT INTO chunks (page_id, text, char_start, char_end)
                    VALUES (?, ?, ?, ?)""",
                    [(page_id, text, start, end) for text, start, end in chunks_by_page[page.page_number]],
                )
        return int(document_id)

    def remove_document(self, path: Path) -> bool:
        with self._connection:
            result = self._connection.execute("DELETE FROM documents WHERE canonical_path = ?", (str(path),))
        return result.rowcount == 1

    def list_chunks(self) -> list[Chunk]:
        rows = self._connection.execute(
            """SELECT chunks.id, documents.id AS document_id, documents.display_name, pages.page_number,
            pages.section, chunks.text, chunks.char_start, chunks.char_end, pages.extraction_method
            FROM chunks
            JOIN pages ON pages.id = chunks.page_id
            JOIN documents ON documents.id = pages.document_id
            ORDER BY chunks.id"""
        ).fetchall()
        return [
            Chunk(
                id=row["id"], document_id=row["document_id"], document_name=row["display_name"],
                page_number=row["page_number"], section=row["section"], text=row["text"],
                char_start=row["char_start"], char_end=row["char_end"], extraction_method=row["extraction_method"],
            )
            for row in rows
        ]

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY,
                    canonical_path TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pages (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    page_number INTEGER NOT NULL CHECK(page_number > 0),
                    section TEXT,
                    extraction_method TEXT NOT NULL,
                    ocr_confidence REAL,
                    UNIQUE(document_id, page_number)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
                    text TEXT NOT NULL CHECK(length(text) > 0),
                    char_start INTEGER NOT NULL CHECK(char_start >= 0),
                    char_end INTEGER NOT NULL CHECK(char_end > char_start)
                );
                CREATE INDEX IF NOT EXISTS chunks_page_id_idx ON chunks(page_id);"""
            )
