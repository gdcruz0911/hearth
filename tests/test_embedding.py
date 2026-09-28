from __future__ import annotations

import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np
from pathlib import Path

from hearth.domain import Chunk
from hearth.embedding import (
    MLX_LOCK, EmbeddingSpec, FlatVectorIndex, IndexBuildCancelled, IndexBuildStopped, IndexBusy,
    IndexCompatibilityError, MLXEmbedder, _build_lock,
)
from hearth.service import HearthService



def setUpModule() -> None:
    # Index builds read this machine's memory pressure and take a user-wide lock; tests get a steady normal
    # reading and a private lock so they pass whatever else the Mac is doing. Pressure tests patch the reading again.
    lock_directory = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(lock_directory.cleanup)
    for patcher in (
        mock.patch("hearth.embedding._memory_signals", return_value=(1, 0.0)),
        mock.patch("hearth.embedding._BUILD_LOCK_PATH", Path(lock_directory.name) / "build.lock"),
    ):
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)

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


def _fake_mlx(model: object, tokenizer: object) -> tuple[types.SimpleNamespace, object]:
    """Stand-ins for mlx.core and mlx_lm backed by NumPy, so these tests run where MLX is not installed, as in CI."""
    core = types.SimpleNamespace(
        array=np.array, ones=np.ones, float32=np.float32, eval=lambda *arrays: None, cache_limits=[],
    )
    core.set_cache_limit = core.cache_limits.append
    loader = types.SimpleNamespace(load=lambda path: (model, tokenizer))
    patch = mock.patch.dict(sys.modules, {"mlx": types.SimpleNamespace(core=core), "mlx.core": core, "mlx_lm": loader})
    return core, patch


def _model_directory(test: unittest.TestCase) -> Path:
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    (Path(directory.name) / "config.json").write_text('{"hidden_size": 2}', encoding="utf-8")
    (Path(directory.name) / "model.safetensors").write_bytes(b"weights")
    return Path(directory.name)


class _Tokenizer:
    def __init__(self, token_ids: list[int]) -> None:
        self._token_ids = token_ids

    def encode(self, text: str, add_special_tokens: bool) -> list[int]:
        return self._token_ids

    def convert_tokens_to_ids(self, token: str) -> int:
        return {"<|endoftext|>": 9}[token]


class _EchoModel:
    """Echoes each token ID as its own hidden state, so the pooled vector names the pooled token."""

    def __init__(self) -> None:
        self.seen: list[int] = []

    def model(self, ids):
        assert MLX_LOCK.locked(), "the forward pass must hold the shared MLX lock"
        self.seen = ids[0].tolist()
        return np.array([[[float(token_id), 1.0] for token_id in self.seen]])


class MLXEmbedderTests(unittest.TestCase):
    def test_pools_the_hidden_state_at_endoftext_under_the_mlx_lock(self) -> None:
        model = _EchoModel()
        core, patch = _fake_mlx(model, _Tokenizer([5, 6]))

        with patch:
            vector = MLXEmbedder(_model_directory(self)).embed(["two tokens"])[0]

        self.assertEqual(model.seen, [5, 6, 9])
        self.assertAlmostEqual(vector[0] / vector[1], 9.0, places=5)
        self.assertFalse(MLX_LOCK.locked())

    def test_long_input_is_truncated_but_still_ends_with_endoftext(self) -> None:
        model = _EchoModel()
        core, patch = _fake_mlx(model, _Tokenizer(list(range(10, 3010))))

        with patch:
            MLXEmbedder(_model_directory(self)).embed(["a very long chunk"])

        self.assertEqual((len(model.seen), model.seen[-1]), (2048, 9))

    def test_loading_the_model_caps_the_mlx_buffer_cache(self) -> None:
        core, patch = _fake_mlx(_EchoModel(), _Tokenizer([5]))

        with patch:
            MLXEmbedder(_model_directory(self)).embed(["text"])

        self.assertEqual(core.cache_limits, [256 * 1024 * 1024])


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
        lock = mock.patch("hearth.embedding._BUILD_LOCK_PATH", Path(self.temporary_directory.name) / "build.lock")
        lock.start()
        self.addCleanup(lock.stop)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_a_build_is_refused_while_another_holds_the_lock_even_for_another_index_folder(self) -> None:
        other = FlatVectorIndex(Path(self.temporary_directory.name) / "other-index", self.embedder)
        self.index.rebuild([_chunk(1, "first")])

        with _build_lock():
            with self.assertRaisesRegex(IndexBusy, "Another Hearth semantic-index build is running"):
                other.rebuild([_chunk(1, "first")])
            with self.assertRaises(IndexBusy):
                self.index.rebuild([_chunk(2, "second")])

        self.assertTrue(self.index.is_current([_chunk(1, "first")]))
        other.rebuild([_chunk(1, "first")])
        self.assertTrue(other.is_current([_chunk(1, "first")]))

    def test_the_lock_is_released_after_a_failed_or_cancelled_build(self) -> None:
        with self.assertRaises(KeyError):
            self.index.rebuild([_chunk(1, "text the embedder does not know")])
        with self.assertRaises(IndexBuildCancelled):
            self.index.rebuild([_chunk(1, "first")], on_progress=lambda *_: None, is_cancelled=iter([False, True]).__next__)

        self.index.rebuild([_chunk(1, "first")])

        self.assertTrue(self.index.is_current([_chunk(1, "first")]))

    def test_memory_pressure_stops_the_build_between_batches_and_keeps_the_active_index(self) -> None:
        self.index.rebuild([_chunk(1, "first")])
        self.index._BUILD_BATCH_SIZE = 1
        replacement = [_chunk(1, "first"), _chunk(2, "second")]
        cases = (
            ([(1, 100.0), (1, 100.0), (2, 100.0)], "macOS reported memory pressure"),
            ([(1, 100.0), (1, 100.0), (1, 1200.0)], "swap grew by 1100 MB"),
        )
        for signals, reason in cases:
            with self.subTest(reason=reason), mock.patch("hearth.embedding._memory_signals", side_effect=signals):
                with self.assertRaisesRegex(IndexBuildStopped, reason + ".*The active index is unchanged"):
                    self.index.rebuild(replacement)

            self.assertTrue(self.index.is_current([_chunk(1, "first")]))
            self.assertEqual([path.name for path in (self.index_directory / "versions").iterdir() if path.name.startswith(".")], [])

    def test_unreadable_memory_signals_warn_once_that_the_build_is_unprotected(self) -> None:
        import contextlib
        import io

        self.index._BUILD_BATCH_SIZE = 1
        chunks = [_chunk(1, "first"), _chunk(2, "second")]
        warnings: list[str] = []
        with mock.patch("hearth.embedding._memory_signals", return_value=None):
            self.index.rebuild(chunks, on_warning=warnings.append)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.index.rebuild(chunks)

        self.assertEqual(len(warnings), 1)
        self.assertIn("not protected by the memory check", warnings[0])
        self.assertEqual(stderr.getvalue().count("not protected by the memory check"), 1)
        self.assertTrue(self.index.is_current(chunks))

    def test_only_marked_staging_from_a_dead_locked_build_is_removed(self) -> None:
        versions = self.index_directory / "versions"
        marked = versions / f".staging-{'a' * 32}"
        legacy = versions / f".staging-{'b' * 32}"
        for folder in (marked, legacy):
            folder.mkdir(parents=True)
            (folder / "vectors.f32").write_bytes(b"partial")
        (marked / "hearth-build-staging").write_text("", encoding="utf-8")

        with _build_lock(), self.assertRaises(IndexBusy):
            self.index.rebuild([_chunk(1, "first")])
        self.assertTrue(marked.exists())

        self.index.rebuild([_chunk(1, "first")])

        self.assertFalse(marked.exists())
        self.assertTrue((legacy / "vectors.f32").exists())
        self.assertFalse(any((path / "hearth-build-staging").exists() for path in versions.iterdir()))

    def test_a_document_imported_during_a_build_leaves_the_index_needing_a_rebuild(self) -> None:
        class AnyText(FakeEmbedder):
            def embed(self, texts: list[str]) -> list[list[float]]:
                return [[1.0, 0.0] for _ in texts]

        root = Path(self.temporary_directory.name)
        (root / "first.md").write_text("The first note.", encoding="utf-8")
        (root / "second.md").write_text("The second note.", encoding="utf-8")
        service = HearthService(root / "hearth.sqlite", semantic_index=FlatVectorIndex(root / "service-index", AnyText({})))
        self.addCleanup(service.close)
        service.import_document(str(root / "first.md"))
        imported = []

        def import_mid_build(completed: int, total: int) -> None:
            if not imported:
                imported.append(service.import_document(str(root / "second.md")))

        status = service.rebuild_semantic_index(on_progress=import_mid_build)

        self.assertEqual(len(imported), 1)
        self.assertEqual((status, service.collection_health().semantic_index_status), ("needs reindex", "needs reindex"))
        with self.assertRaises(IndexCompatibilityError):
            service.answer("Which note is second?")

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
