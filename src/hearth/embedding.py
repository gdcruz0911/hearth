from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

import numpy as np

from .chunking import CHUNKING_VERSION
from .domain import Chunk, DocumentRelationship, Evidence


class EmbeddingError(RuntimeError):
    """Raised when local embeddings cannot be produced safely."""


class IndexError(RuntimeError):
    """Raised when a local vector index cannot be built or read safely."""


class IndexCompatibilityError(IndexError):
    """Raised when an index does not match its configured embedding inputs."""


class IndexBuildCancelled(IndexError):
    """Raised when a user stops an in-progress derived index rebuild."""


@dataclass(frozen=True)
class EmbeddingSpec:
    model_name: str
    model_fingerprint: str
    dimension: int
    pooling: str
    normalization: str


class Embedder(Protocol):
    @property
    def spec(self) -> EmbeddingSpec: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class MLXEmbedder:
    """Embeds text with one pre-provisioned local MLX Qwen3 embedding model."""

    def __init__(self, model_directory: Path):
        self._model_directory = model_directory.expanduser().resolve()
        config_path = self._model_directory / "config.json"
        weights_path = self._model_directory / "model.safetensors"
        if not self._model_directory.is_dir() or not config_path.is_file() or not weights_path.is_file():
            raise EmbeddingError("A local MLX embedding model directory with config.json and model.safetensors is required.")
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            dimension = config["hidden_size"]
            model_fingerprint = _sha256_file(weights_path)
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise EmbeddingError("The local MLX embedding model configuration is invalid.") from exc
        if not isinstance(dimension, int) or dimension < 1:
            raise EmbeddingError("The local MLX embedding model dimension is invalid.")
        self._spec = EmbeddingSpec(
            model_name=self._model_directory.name,
            model_fingerprint=model_fingerprint,
            dimension=dimension,
            pooling="last-token",
            normalization="l2",
        )
        self._model = None
        self._tokenizer = None

    @property
    def spec(self) -> EmbeddingSpec:
        return self._spec

    def embed(self, texts: list[str]) -> list[list[float]]:
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError("Embedding input must contain non-empty text.")
        os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            import mlx.core as mx
            from mlx_lm import load
        except ImportError as exc:
            raise EmbeddingError("Local embeddings require the optional MLX runtime.") from exc
        try:
            if self._model is None or self._tokenizer is None:
                self._model, self._tokenizer = load(str(self._model_directory))
            hidden_model = getattr(self._model, "model", None)
            if hidden_model is None:
                raise EmbeddingError("The local MLX model does not expose embedding hidden states.")
            vectors = []
            for text in texts:
                token_ids = self._tokenizer.encode(text, add_special_tokens=False)
                if not token_ids:
                    raise EmbeddingError("Embedding input produced no tokens.")
                hidden_states = hidden_model(mx.array(token_ids)[None])
                vector = hidden_states[0, -1].astype(mx.float32)
                mx.eval(vector)
                vectors.append(_normalize_vector(vector.tolist(), self._spec.dimension))
            return vectors
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError("Local MLX embedding failed.") from exc


class FlatVectorIndex:
    """A versioned local flat index with atomic activation and strict manifest checks."""

    _FORMAT = "hearth-flat-vector-index-v1"
    _VECTORS_FILE = "vectors.f32"
    _BUILD_BATCH_SIZE = 8

    def __init__(self, index_directory: Path, embedder: Embedder):
        self._index_directory = index_directory.expanduser().resolve()
        self._embedder = embedder
        self._relationship_cache: dict[tuple[str, int, float], tuple[DocumentRelationship, ...]] = {}

    def rebuild(
        self,
        chunks: list[Chunk],
        *,
        on_progress: Callable[[int, int], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """Build an index in small, cancellable batches before atomically activating it."""
        ordered_chunks = sorted(chunks, key=lambda chunk: chunk.id)
        if len({chunk.id for chunk in ordered_chunks}) != len(ordered_chunks):
            raise IndexError("Cannot build an index with duplicate chunk IDs.")
        _raise_if_cancelled(is_cancelled)
        total = len(ordered_chunks)
        if on_progress is not None:
            on_progress(0, total)
        self._index_directory.mkdir(parents=True, exist_ok=True)
        versions_directory = self._index_directory / "versions"
        versions_directory.mkdir(parents=True, exist_ok=True)
        previous_version = self._active_version()
        version = uuid4().hex
        staging_directory = versions_directory / f".staging-{version}"
        final_directory = versions_directory / version
        try:
            _raise_if_cancelled(is_cancelled)
            staging_directory.mkdir()
            vectors_path = staging_directory / self._VECTORS_FILE
            completed = 0
            with vectors_path.open("wb") as target:
                for start in range(0, total, self._BUILD_BATCH_SIZE):
                    _raise_if_cancelled(is_cancelled)
                    batch = ordered_chunks[start : start + self._BUILD_BATCH_SIZE]
                    batch_vectors = self._embedder.embed([chunk.text for chunk in batch])
                    if len(batch_vectors) != len(batch):
                        raise IndexError("Embedding output count did not match the chunk count.")
                    for vector in batch_vectors:
                        normalized_vector = _normalize_vector(vector, self._embedder.spec.dimension)
                        target.write(struct.pack(f"<{self._embedder.spec.dimension}f", *normalized_vector))
                    completed += len(batch)
                    if on_progress is not None:
                        on_progress(completed, total)
                target.flush()
                os.fsync(target.fileno())
            manifest = self._manifest(version, ordered_chunks)
            _write_json(staging_directory / "manifest.json", manifest)
            _raise_if_cancelled(is_cancelled)
            os.replace(staging_directory, final_directory)
            _write_json_atomically(self._index_directory / "active.json", {"version": version})
        except Exception:
            if staging_directory.exists():
                shutil.rmtree(staging_directory)
            raise
        if previous_version is not None and previous_version != version:
            self._remove_version(previous_version)

    def search(self, question: str, chunks: list[Chunk], limit: int = 20) -> list[Evidence]:
        if limit < 1:
            return []
        if not chunks:
            return []
        if not question.strip():
            return []
        manifest, vectors_path = self._load_manifest(chunks)
        query_vector = self._embedder.embed([question])[0]
        query_vector = _normalize_vector(query_vector, self._embedder.spec.dimension)
        vectors = _read_vectors(vectors_path, len(manifest["chunk_ids"]), self._embedder.spec.dimension)
        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        scores = vectors @ np.asarray(query_vector, dtype="<f4")
        results = [
            Evidence(chunk=chunks_by_id[chunk_id], score=float(score))
            for chunk_id, score in zip(manifest["chunk_ids"], scores)
        ]
        return sorted(results, key=lambda item: (-item.score, item.chunk.id))[:limit]

    def is_current(self, chunks: list[Chunk]) -> bool:
        """Returns whether the active derived index matches the current local chunks."""
        try:
            self._load_manifest(chunks)
        except IndexCompatibilityError:
            return False
        return True

    def document_relationships(
        self, chunks: list[Chunk], limit: int = 12, minimum_score: float = 0.72
    ) -> list[DocumentRelationship]:
        """Return bounded evidence-backed document relationships from chunk similarity.

        An edge is admitted when two chunks in different documents are each other's
        strongest cross-document match and score at or above the minimum (ADR-0017).
        Mutual matching is what stops a long multi-topic document, which holds some
        chunk close to almost anything, from connecting to the whole collection.
        The admitted score is the score displayed; there is no separate gate.
        Each source then nominates its qualifying neighbors so the bounded visible
        set spans many neighborhoods instead of one globally dominant cluster.
        """
        if limit < 1 or not chunks:
            return []
        if not 0 <= minimum_score <= 1:
            raise ValueError("minimum_score must be between zero and one.")

        manifest, vectors_path = self._load_manifest(chunks)
        cache_key = (str(manifest["version"]), limit, minimum_score)
        cached = self._relationship_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        vectors = _read_vectors(vectors_path, len(manifest["chunk_ids"]), self._embedder.spec.dimension)
        ordered_chunks = [chunks_by_id[chunk_id] for chunk_id in manifest["chunk_ids"]]
        best_by_document_pair = _mutual_best_chunk_pairs(ordered_chunks, vectors, minimum_score)

        candidates_by_document: dict[int, list[tuple[float, int, int]]] = {}
        for (left_document_id, right_document_id), (score, _, _) in best_by_document_pair.items():
            candidate = (score, left_document_id, right_document_id)
            candidates_by_document.setdefault(left_document_id, []).append(candidate)
            candidates_by_document.setdefault(right_document_id, []).append(candidate)

        candidate_pairs = _distributed_relationship_candidates(candidates_by_document, limit)
        relationships = []
        for _, left_document_id, right_document_id in candidate_pairs:
            score, left_chunk_id, right_chunk_id = best_by_document_pair[(left_document_id, right_document_id)]
            relationships.append(
                DocumentRelationship(chunks_by_id[left_chunk_id], chunks_by_id[right_chunk_id], score)
            )
        result = tuple(
            sorted(
                relationships,
                key=lambda item: (-item.score, item.left_chunk.document_id, item.right_chunk.document_id),
            )
        )
        self._relationship_cache = {
            key: value for key, value in self._relationship_cache.items() if key[0] == cache_key[0]
        }
        self._relationship_cache[cache_key] = result
        return list(result)

    def _manifest(self, version: str, chunks: list[Chunk]) -> dict[str, object]:
        spec = self._embedder.spec
        return {
            "format": self._FORMAT,
            "version": version,
            "model_name": spec.model_name,
            "model_fingerprint": spec.model_fingerprint,
            "dimension": spec.dimension,
            "pooling": spec.pooling,
            "normalization": spec.normalization,
            "chunking_version": CHUNKING_VERSION,
            "chunk_ids": [chunk.id for chunk in chunks],
            "vectors_file": self._VECTORS_FILE,
        }

    def _load_manifest(self, chunks: list[Chunk]) -> tuple[dict[str, object], Path]:
        version = self._active_version()
        if version is None:
            raise IndexCompatibilityError("No local semantic index is active. Import or reindex a document first.")
        manifest_path = self._index_directory / "versions" / version / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IndexCompatibilityError("The active local semantic index manifest is unreadable.") from exc
        if not isinstance(manifest, dict):
            raise IndexCompatibilityError("The active local semantic index manifest is invalid.")
        expected = self._manifest(version, sorted(chunks, key=lambda chunk: chunk.id))
        fields = (
            "format",
            "model_name",
            "model_fingerprint",
            "dimension",
            "pooling",
            "normalization",
            "chunking_version",
            "chunk_ids",
            "vectors_file",
        )
        if any(manifest.get(field) != expected[field] for field in fields):
            raise IndexCompatibilityError("The active local semantic index does not match the current model or chunks. Reindex first.")
        vectors_path = self._index_directory / "versions" / version / self._VECTORS_FILE
        if not vectors_path.is_file():
            raise IndexCompatibilityError("The active local semantic index vectors are missing.")
        return manifest, vectors_path

    def _active_version(self) -> str | None:
        active_path = self._index_directory / "active.json"
        if not active_path.exists():
            return None
        try:
            payload = json.loads(active_path.read_text(encoding="utf-8"))
            version = payload["version"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise IndexCompatibilityError("The active local semantic index pointer is invalid.") from exc
        if not isinstance(version, str) or not version.isalnum():
            raise IndexCompatibilityError("The active local semantic index pointer is invalid.")
        return version

    def _remove_version(self, version: str) -> None:
        candidate = self._index_directory / "versions" / version
        if not candidate.is_dir() or candidate.is_symlink():
            raise IndexError("The previous local semantic index version could not be removed safely.")
        try:
            shutil.rmtree(candidate)
        except OSError as exc:
            raise IndexError("The previous local semantic index version could not be removed.") from exc


def _normalize_vector(vector: list[float], dimension: int) -> list[float]:
    if len(vector) != dimension or any(not math.isfinite(value) for value in vector):
        raise EmbeddingError("Embedding output has an invalid dimension or value.")
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        raise EmbeddingError("Embedding output must not be a zero vector.")
    return [value / magnitude for value in vector]


def _raise_if_cancelled(is_cancelled: Callable[[], bool] | None) -> None:
    if is_cancelled is not None and is_cancelled():
        raise IndexBuildCancelled("The local semantic-index rebuild was cancelled.")


# How many cross-document neighbors a chunk may call its own before mutuality is
# required. 1 would yield a matching, not a graph: at most N/2 edges total.
# ponytail: a tuning parameter with no corpus behind it yet. The evaluation
# corpus should set it; until then this is a defensible default, not a measured one.
_MUTUAL_NEIGHBORS = 5


def _mutual_best_chunk_pairs(
    ordered_chunks: list[Chunk], vectors: np.ndarray, minimum_score: float
) -> dict[tuple[int, int], tuple[float, int, int]]:
    """Find cross-document chunk pairs that each rank among the other's nearest.

    Vectors are L2-normalized, so the dot product is cosine similarity. Requiring
    the nearness to be mutual is what stops a long multi-topic document, which
    holds some chunk close to almost anything, from connecting to the whole
    collection (ADR-0017).
    """
    total = len(ordered_chunks)
    document_ids = np.array([chunk.document_id for chunk in ordered_chunks])
    if total < 2 or len(set(document_ids.tolist())) < 2:
        return {}

    # ponytail: materializes the N x N similarity matrix plus boolean masks.
    # Measured: 15 MB at 1000 chunks, 844 MB and 0.9 s at 7500, quadratic from
    # there, so roughly 3.4 GB at 15000 is where it stops being reasonable.
    # Switch to a blocked top-k pass at that point; the admitted edges do not change.
    similarity = np.asarray(vectors @ vectors.T, dtype=np.float32)
    similarity[document_ids[:, None] == document_ids[None, :]] = -np.inf

    neighbors = min(_MUTUAL_NEIGHBORS, total - 1)
    # Partition for the k largest directly; negating would copy the whole matrix.
    nearest = np.argpartition(similarity, kth=total - neighbors, axis=1)[:, -neighbors:]
    is_near = np.zeros(similarity.shape, dtype=bool)
    np.put_along_axis(is_near, nearest, True, axis=1)
    mutual = is_near & is_near.T
    mutual &= similarity >= minimum_score

    pairs: dict[tuple[int, int], tuple[float, int, int]] = {}
    for left, right in zip(*np.nonzero(mutual)):
        if left >= right:  # each mutual pair appears twice
            continue
        score = float(similarity[left, right])
        left_chunk, right_chunk = ordered_chunks[left], ordered_chunks[right]
        key = (left_chunk.document_id, right_chunk.document_id)
        if key[0] > key[1]:
            key = (key[1], key[0])
            left_chunk, right_chunk = right_chunk, left_chunk
        existing = pairs.get(key)
        if existing is None or score > existing[0]:
            pairs[key] = (score, left_chunk.id, right_chunk.id)
    return pairs


def _distributed_relationship_candidates(
    candidates_by_document: dict[int, list[tuple[float, int, int]]], limit: int
) -> list[tuple[float, int, int]]:
    """Choose links round-robin so the map represents many local neighborhoods."""
    ordered_by_document = {
        document_id: sorted(candidates, key=lambda item: (-item[0], item[1], item[2]))
        for document_id, candidates in candidates_by_document.items()
    }
    selected: list[tuple[float, int, int]] = []
    selected_pairs: set[tuple[int, int]] = set()
    rank = 0
    while len(selected) < limit:
        added_at_rank = False
        for document_id in sorted(ordered_by_document):
            candidates = ordered_by_document[document_id]
            if rank >= len(candidates):
                continue
            candidate = candidates[rank]
            pair = (candidate[1], candidate[2])
            if pair in selected_pairs:
                continue
            selected.append(candidate)
            selected_pairs.add(pair)
            added_at_rank = True
            if len(selected) == limit:
                break
        if not added_at_rank:
            break
        rank += 1
    return selected


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _read_vectors(path: Path, count: int, dimension: int) -> np.ndarray:
    expected_size = count * dimension * 4
    payload = path.read_bytes()
    if len(payload) != expected_size:
        raise IndexCompatibilityError("The active local semantic index vector file has an unexpected size.")
    return np.frombuffer(payload, dtype="<f4").reshape(count, dimension)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    with path.open("w", encoding="utf-8") as target:
        json.dump(payload, target, sort_keys=True, separators=(",", ":"))
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    temporary_path = path.with_name(f".{path.name}-{uuid4().hex}")
    try:
        _write_json(temporary_path, payload)
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
