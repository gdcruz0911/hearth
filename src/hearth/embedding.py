from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
from typing import Protocol
from uuid import uuid4

from .chunking import CHUNKING_VERSION
from .domain import Chunk, Evidence


class EmbeddingError(RuntimeError):
    """Raised when local embeddings cannot be produced safely."""


class IndexError(RuntimeError):
    """Raised when a local vector index cannot be built or read safely."""


class IndexCompatibilityError(IndexError):
    """Raised when an index does not match its configured embedding inputs."""


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

    def __init__(self, index_directory: Path, embedder: Embedder):
        self._index_directory = index_directory.expanduser().resolve()
        self._embedder = embedder

    def rebuild(self, chunks: list[Chunk]) -> None:
        ordered_chunks = sorted(chunks, key=lambda chunk: chunk.id)
        if len({chunk.id for chunk in ordered_chunks}) != len(ordered_chunks):
            raise IndexError("Cannot build an index with duplicate chunk IDs.")
        vectors = self._embedder.embed([chunk.text for chunk in ordered_chunks]) if ordered_chunks else []
        if len(vectors) != len(ordered_chunks):
            raise IndexError("Embedding output count did not match the chunk count.")
        normalized_vectors = [_normalize_vector(vector, self._embedder.spec.dimension) for vector in vectors]
        self._index_directory.mkdir(parents=True, exist_ok=True)
        versions_directory = self._index_directory / "versions"
        versions_directory.mkdir(parents=True, exist_ok=True)
        previous_version = self._active_version()
        version = uuid4().hex
        staging_directory = versions_directory / f".staging-{version}"
        final_directory = versions_directory / version
        try:
            staging_directory.mkdir()
            vectors_path = staging_directory / self._VECTORS_FILE
            _write_vectors(vectors_path, normalized_vectors, self._embedder.spec.dimension)
            manifest = self._manifest(version, ordered_chunks)
            _write_json(staging_directory / "manifest.json", manifest)
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
        results = []
        for chunk_id, vector in zip(manifest["chunk_ids"], vectors):
            score = sum(left * right for left, right in zip(query_vector, vector))
            results.append(Evidence(chunk=chunks_by_id[chunk_id], score=score))
        return sorted(results, key=lambda item: (-item.score, item.chunk.id))[:limit]

    def is_current(self, chunks: list[Chunk]) -> bool:
        """Returns whether the active derived index matches the current local chunks."""
        try:
            self._load_manifest(chunks)
        except IndexCompatibilityError:
            return False
        return True

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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_vectors(path: Path, vectors: list[list[float]], dimension: int) -> None:
    with path.open("wb") as target:
        for vector in vectors:
            target.write(struct.pack(f"<{dimension}f", *vector))
        target.flush()
        os.fsync(target.fileno())


def _read_vectors(path: Path, count: int, dimension: int) -> list[list[float]]:
    expected_size = count * dimension * 4
    payload = path.read_bytes()
    if len(payload) != expected_size:
        raise IndexCompatibilityError("The active local semantic index vector file has an unexpected size.")
    return [
        list(struct.unpack_from(f"<{dimension}f", payload, offset * dimension * 4))
        for offset in range(count)
    ]


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
