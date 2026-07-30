from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hearth.domain import Chunk
from hearth.embedding import EmbeddingSpec, FlatVectorIndex, IndexCompatibilityError


class FakeEmbedder:
    spec = EmbeddingSpec(
        model_name="synthetic-embedding-model",
        model_fingerprint="synthetic-fingerprint",
        dimension=2,
        pooling="last-token",
        normalization="l2",
    )

    def __init__(self, vectors: dict[str, list[float]]):
        self._vectors = vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors[text] for text in texts]


def _chunk(chunk_id: int, text: str) -> Chunk:
    return Chunk(chunk_id, 1, "fixture.md", 1, "Fixture", text, 0, len(text), "native", None)


class FlatVectorIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.index_directory = Path(self.temporary_directory.name) / "index"
        self.embedder = FakeEmbedder(
            {
                "first": [3.0, 0.0],
                "second": [2.0, 0.0],
                "other": [0.0, 4.0],
                "query": [1.0, 0.0],
            }
        )
        self.index = FlatVectorIndex(self.index_directory, self.embedder)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_search_normalizes_vectors_and_breaks_ties_by_chunk_id(self) -> None:
        chunks = [_chunk(2, "second"), _chunk(1, "first"), _chunk(3, "other")]
        self.index.rebuild(chunks)

        results = self.index.search("query", chunks)

        self.assertEqual([item.chunk.id for item in results], [1, 2, 3])
        self.assertAlmostEqual(results[0].score, 1.0)
        self.assertAlmostEqual(results[2].score, 0.0)

    def test_search_rejects_manifest_chunk_id_mismatch(self) -> None:
        self.index.rebuild([_chunk(1, "first")])

        with self.assertRaisesRegex(IndexCompatibilityError, "does not match"):
            self.index.search("query", [_chunk(2, "second")])

    def test_rebuild_activates_new_version_and_removes_previous_version(self) -> None:
        self.index.rebuild([_chunk(1, "first")])
        self.index.rebuild([_chunk(2, "second")])

        versions = [path for path in (self.index_directory / "versions").iterdir() if path.is_dir()]
        results = self.index.search("query", [_chunk(2, "second")])

        self.assertEqual(len(versions), 1)
        self.assertEqual([item.chunk.id for item in results], [2])

    def test_search_requires_an_active_index(self) -> None:
        with self.assertRaisesRegex(IndexCompatibilityError, "No local semantic index is active"):
            self.index.search("query", [_chunk(1, "first")])

    def test_is_current_reports_missing_or_mismatched_active_index(self) -> None:
        first_chunk = _chunk(1, "first")
        self.assertFalse(self.index.is_current([first_chunk]))

        self.index.rebuild([first_chunk])

        self.assertTrue(self.index.is_current([first_chunk]))
        self.assertFalse(self.index.is_current([_chunk(2, "second")]))
