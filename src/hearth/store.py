from __future__ import annotations

import sqlite3
from pathlib import Path

from .chunking import CHUNKING_VERSION
from .domain import (
    CollectionHealth,
    Chunk,
    ChunkInspection,
    DocumentInspection,
    ExtractedPage,
    ImportedDocument,
    PageInspection,
    SourceAttention,
)


class SQLiteStore:
    """Authoritative local metadata store. Retrieval indexes are derived data."""

    def __init__(self, database_path: Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

    def replace_document(
        self,
        path: Path,
        pages: tuple[ExtractedPage, ...],
        chunks_by_page: dict[int, list[tuple[str, int, int]]],
        source_fingerprint: str,
        source_size: int,
        source_mtime_ns: int,
    ) -> int:
        with self._connection:
            self._connection.execute("DELETE FROM documents WHERE canonical_path = ?", (str(path),))
            document_id = self._connection.execute(
                """INSERT INTO documents
                (canonical_path, display_name, source_fingerprint, source_size, source_mtime_ns,
                 chunking_version)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (str(path), path.name, source_fingerprint, source_size, source_mtime_ns, CHUNKING_VERSION),
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

    def remove_document_by_id(self, document_id: int) -> bool:
        """Remove one local collection record without touching its source file."""
        with self._connection:
            result = self._connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        return result.rowcount == 1

    def list_documents(self) -> list[ImportedDocument]:
        rows = self._connection.execute(
            """SELECT documents.id, documents.display_name, COUNT(DISTINCT pages.id) AS page_count,
            COUNT(chunks.id) AS chunk_count,
            COUNT(DISTINCT CASE WHEN pages.extraction_method = 'ocr' THEN pages.id END) AS ocr_page_count
            FROM documents
            LEFT JOIN pages ON pages.document_id = documents.id
            LEFT JOIN chunks ON chunks.page_id = pages.id
            GROUP BY documents.id
            ORDER BY documents.id"""
        ).fetchall()
        return [
            ImportedDocument(
                id=row["id"],
                name=row["display_name"],
                page_count=row["page_count"],
                chunk_count=row["chunk_count"],
                ocr_page_count=row["ocr_page_count"],
            )
            for row in rows
        ]

    def collection_health(
        self, semantic_index_status: str, source_attention: tuple[SourceAttention, ...]
    ) -> CollectionHealth:
        row = self._connection.execute(
            """SELECT COUNT(DISTINCT documents.id) AS document_count,
            COUNT(DISTINCT pages.id) AS page_count,
            COUNT(chunks.id) AS chunk_count,
            COUNT(DISTINCT CASE WHEN pages.extraction_method = 'ocr' THEN pages.id END) AS ocr_page_count
            FROM documents
            LEFT JOIN pages ON pages.document_id = documents.id
            LEFT JOIN chunks ON chunks.page_id = pages.id"""
        ).fetchone()
        return CollectionHealth(
            document_count=row["document_count"],
            page_count=row["page_count"],
            chunk_count=row["chunk_count"],
            ocr_page_count=row["ocr_page_count"],
            unavailable_source_count=sum(item.status == "source unavailable" for item in source_attention),
            changed_source_count=sum(item.status == "source changed since import" for item in source_attention),
            baseline_reindex_count=sum(item.status == "source needs baseline reindex" for item in source_attention),
            stale_chunking_count=sum(item.status == "chunking outdated" for item in source_attention),
            semantic_index_status=semantic_index_status,
            source_attention=source_attention,
        )

    def stale_chunking_document_ids(self) -> set[int]:
        """Documents whose stored chunks predate the current chunker (ADR-0018)."""
        rows = self._connection.execute(
            "SELECT id FROM documents WHERE chunking_version IS NULL OR chunking_version != ?",
            (CHUNKING_VERSION,),
        ).fetchall()
        return {row["id"] for row in rows}

    def source_records(self) -> list[tuple[int, str, Path, str | None, int | None, int | None]]:
        rows = self._connection.execute(
            """SELECT id, display_name, canonical_path, source_fingerprint, source_size, source_mtime_ns
            FROM documents ORDER BY id"""
        ).fetchall()
        return [
            (
                row["id"],
                row["display_name"],
                Path(row["canonical_path"]),
                row["source_fingerprint"],
                row["source_size"],
                row["source_mtime_ns"],
            )
            for row in rows
        ]

    def source_path(self, document_id: int) -> Path | None:
        row = self._connection.execute(
            "SELECT canonical_path FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        return Path(row["canonical_path"]) if row is not None else None

    def inspect_document(self, document_id: int) -> DocumentInspection | None:
        document_row = self._connection.execute(
            """SELECT documents.id, documents.display_name, COUNT(DISTINCT pages.id) AS page_count,
            COUNT(chunks.id) AS chunk_count,
            COUNT(DISTINCT CASE WHEN pages.extraction_method = 'ocr' THEN pages.id END) AS ocr_page_count
            FROM documents
            LEFT JOIN pages ON pages.document_id = documents.id
            LEFT JOIN chunks ON chunks.page_id = pages.id
            WHERE documents.id = ?
            GROUP BY documents.id""",
            (document_id,),
        ).fetchone()
        if document_row is None:
            return None
        page_rows = self._connection.execute(
            """SELECT pages.id AS page_id, pages.page_number, pages.section, pages.extraction_method,
            pages.ocr_confidence, chunks.id AS chunk_id, chunks.char_start, chunks.char_end
            FROM pages
            LEFT JOIN chunks ON chunks.page_id = pages.id
            WHERE pages.document_id = ?
            ORDER BY pages.page_number, chunks.id""",
            (document_id,),
        ).fetchall()
        pages: list[PageInspection] = []
        for row in page_rows:
            if not pages or pages[-1].page_number != row["page_number"]:
                pages.append(
                    PageInspection(
                        page_number=row["page_number"],
                        section=row["section"],
                        extraction_method=row["extraction_method"],
                        ocr_confidence=row["ocr_confidence"],
                        chunks=(),
                    )
                )
            if row["chunk_id"] is not None:
                page = pages[-1]
                pages[-1] = PageInspection(
                    page_number=page.page_number,
                    section=page.section,
                    extraction_method=page.extraction_method,
                    ocr_confidence=page.ocr_confidence,
                    chunks=page.chunks + (
                        ChunkInspection(
                            id=row["chunk_id"],
                            char_start=row["char_start"],
                            char_end=row["char_end"],
                        ),
                    ),
                )
        return DocumentInspection(
            document=ImportedDocument(
                id=document_row["id"],
                name=document_row["display_name"],
                page_count=document_row["page_count"],
                chunk_count=document_row["chunk_count"],
                ocr_page_count=document_row["ocr_page_count"],
            ),
            pages=tuple(pages),
        )

    def list_chunks(self) -> list[Chunk]:
        rows = self._connection.execute(
            """SELECT chunks.id, documents.id AS document_id, documents.display_name, pages.page_number,
            pages.section, chunks.text, chunks.char_start, chunks.char_end, pages.extraction_method,
            pages.ocr_confidence
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
                ocr_confidence=row["ocr_confidence"],
            )
            for row in rows
        ]

    def keyword_search(self, terms: set[str], limit: int = 20) -> list[int]:
        """Chunk IDs ranked by BM25 over any of the terms, best first."""
        if not terms or limit < 1:
            return []
        # Terms are letters and digits only, so quoting each one keeps FTS5 query syntax out of the question.
        query = " OR ".join(f'"{term}"' for term in sorted(terms))
        rows = self._connection.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank, rowid LIMIT ?", (query, limit)
        ).fetchall()
        return [row[0] for row in rows]

    def _create_schema(self) -> None:
        keyword_index_exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'chunks_fts'"
        ).fetchone() is not None
        with self._connection:
            self._connection.executescript(
                """CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY,
                    canonical_path TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    source_fingerprint TEXT,
                    source_size INTEGER,
                    source_mtime_ns INTEGER
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
                CREATE INDEX IF NOT EXISTS chunks_page_id_idx ON chunks(page_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text, content='chunks', content_rowid='id', tokenize='porter unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
                END;"""
            )
            if not keyword_index_exists:  # Collections imported before the keyword index existed.
                self._connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
            document_columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(documents)")}
            for column, definition in (
                ("source_fingerprint", "TEXT"),
                ("source_size", "INTEGER"),
                ("source_mtime_ns", "INTEGER"),
                ("chunking_version", "TEXT"),
            ):
                if column not in document_columns:
                    self._connection.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")
