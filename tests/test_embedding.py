from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hearth.domain import Chunk
from hearth.embedding import EmbeddingSpec, FlatVectorIndex, IndexBuildCancelled, IndexCompatibilityError


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


def _document_chunk(chunk_id: int, document_id: int, text: str) -> Chunk:
    return Chunk(chunk_id, document_id, f"fixture-{document_id}.md", 1, "Fixture", text, 0, len(text), "native", None)


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

    def test_document_relationships_use_document_centroids_then_return_chunk_evidence(self) -> None:
        chunks = [
            _document_chunk(1, 1, "first"),
            _document_chunk(2, 2, "second"),
            _document_chunk(3, 3, "other"),
        ]
        self.index.rebuild(chunks)

        relationships = self.index.document_relationships(chunks, limit=1, minimum_score=0.7)

        self.assertEqual(len(relationships), 1)
        self.assertEqual(
            (relationships[0].left_chunk.document_id, relationships[0].right_chunk.document_id),
            (1, 2),
        )
        self.assertAlmostEqual(relationships[0].score, 1.0)

    def test_document_relationships_distribute_links_across_sources(self) -> None:
        self.embedder._vectors.update(
            {
                "third": [1.0, 0.0],
                "fourth": [0.0, 1.0],
            }
        )
        chunks = [
            _document_chunk(1, 1, "first"),
            _document_chunk(2, 2, "second"),
            _document_chunk(3, 3, "third"),
            _document_chunk(4, 4, "fourth"),
        ]
        self.index.rebuild(chunks)

        relationships = self.index.document_relationships(chunks, limit=2, minimum_score=0.7)

        self.assertEqual(
            {(item.left_chunk.document_id, item.right_chunk.document_id) for item in relationships},
            {(1, 2), (1, 3)},
        )

    def test_cancelled_rebuild_keeps_the_previous_active_index(self) -> None:
        first_chunk = _chunk(1, "first")
        replacement_chunks = [first_chunk, _chunk(2, "second")]
        self.index.rebuild([first_chunk])
        self.index._BUILD_BATCH_SIZE = 1
        progress: list[int] = []

        with self.assertRaises(IndexBuildCancelled):
            self.index.rebuild(
                replacement_chunks,
                on_progress=lambda completed, total: progress.append(completed),
                is_cancelled=lambda: any(completed >= 1 for completed in progress),
            )

        self.assertEqual(progress, [0, 1])
        self.assertTrue(self.index.is_current([first_chunk]))
        self.assertFalse(self.index.is_current(replacement_chunks))


class ThreeDimensionalEmbedder(FakeEmbedder):
    spec = EmbeddingSpec(
        model_name="synthetic-3d-model",
        model_fingerprint="synthetic-3d-fingerprint",
        dimension=3,
        pooling="last-token",
        normalization="l2",
    )


class ChunkLevelRelationshipTests(unittest.TestCase):
    """ADR-0017: edges come from chunk similarity, not document centroids."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.index_directory = Path(self.temporary_directory.name) / "index"

    def _index(self, vectors: dict[str, list[float]]) -> FlatVectorIndex:
        return FlatVectorIndex(self.index_directory, ThreeDimensionalEmbedder(vectors))

    def test_documents_sharing_one_section_are_linked(self) -> None:
        # Each document has one chunk on a shared topic and two on its own.
        # Their centroids are far apart, so the previous centroid gate dropped
        # this pair before ever comparing the chunks that actually match.
        index = self._index(
            {
                "shared-left": [1.0, 0.0, 0.0],
                "shared-right": [1.0, 0.0, 0.0],
                "left-a": [0.0, 1.0, 0.0],
                "left-b": [0.0, 1.0, 0.0],
                "right-a": [0.0, 0.0, 1.0],
                "right-b": [0.0, 0.0, 1.0],
            }
        )
        chunks = [
            _document_chunk(1, 1, "shared-left"),
            _document_chunk(2, 1, "left-a"),
            _document_chunk(3, 1, "left-b"),
            _document_chunk(4, 2, "shared-right"),
            _document_chunk(5, 2, "right-a"),
            _document_chunk(6, 2, "right-b"),
        ]
        index.rebuild(chunks)

        # Premise: the centroids these documents would produce score far below the
        # minimum, so a centroid gate could not admit this edge at any threshold.
        centroid_similarity = (1 / 5) ** 0.5 * (1 / 5) ** 0.5
        self.assertLess(centroid_similarity, 0.7)

        relationships = index.document_relationships(chunks, limit=12, minimum_score=0.7)

        self.assertEqual(len(relationships), 1)
        relationship = relationships[0]
        self.assertEqual(
            (relationship.left_chunk.document_id, relationship.right_chunk.document_id), (1, 2)
        )
        # The edge carries the chunks that explain it, not an averaged score.
        self.assertEqual((relationship.left_chunk.id, relationship.right_chunk.id), (1, 4))
        self.assertAlmostEqual(relationship.score, 1.0, places=5)

    def test_unrelated_documents_are_not_linked(self) -> None:
        index = self._index({"left": [1.0, 0.0, 0.0], "right": [0.0, 1.0, 0.0]})
        chunks = [_document_chunk(1, 1, "left"), _document_chunk(2, 2, "right")]
        index.rebuild(chunks)

        self.assertEqual(index.document_relationships(chunks, limit=12, minimum_score=0.7), [])

    def test_a_hub_document_does_not_connect_to_everything(self) -> None:
        # One document holds a chunk close to every other document's topic.
        # A one-sided threshold would link it to all of them.
        vectors = {"hub-a": [1.0, 0.0, 0.0], "hub-b": [0.0, 1.0, 0.0], "hub-c": [0.0, 0.0, 1.0]}
        chunks = [_document_chunk(index, 1, name) for index, name in enumerate(vectors, start=1)]
        for offset, (name, vector) in enumerate(list(vectors.items())):
            satellite = f"satellite-{offset}"
            vectors[satellite] = vector
            chunks.append(_document_chunk(10 + offset, 2 + offset, satellite))
        index = self._index(vectors)
        index.rebuild(chunks)

        relationships = index.document_relationships(chunks, limit=12, minimum_score=0.7)
        hub_edges = [
            item
            for item in relationships
            if 1 in (item.left_chunk.document_id, item.right_chunk.document_id)
        ]
        self.assertLessEqual(len(hub_edges), 3)
        for item in relationships:
            self.assertGreaterEqual(item.score, 0.7)
