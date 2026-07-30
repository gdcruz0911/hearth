from __future__ import annotations

import hashlib
import math
import os
import re
from pathlib import Path
from typing import Callable, Protocol

from .domain import Chunk, Evidence


class LocalIndex(Protocol):
    """Local retrieval boundary for the future vector-index implementation."""

    def search(self, question: str, limit: int = 20) -> list[Evidence]: ...


class Reranker(Protocol):
    """Local reranking boundary. Implementations must not send evidence remotely."""

    def rerank(self, question: str, candidates: list[Evidence], limit: int = 6) -> list[Evidence]: ...


class RerankerError(RuntimeError):
    """Raised when local reranking cannot safely score an evidence candidate."""


class HashingVectorIndex:
    """Deterministic, dependency-free local vector index for the initial scaffold."""

    def __init__(self, chunks: list[Chunk], dimensions: int = 1024):
        if dimensions < 1:
            raise ValueError("dimensions must be positive.")
        self._chunks = chunks
        self._dimensions = dimensions
        self._vectors = {chunk.id: _vector(chunk.text, dimensions) for chunk in chunks}

    def search(self, question: str, limit: int = 20) -> list[Evidence]:
        query = _vector(question, self._dimensions)
        query_magnitude = _magnitude(query)
        if not query_magnitude:
            return []
        results = []
        for chunk in self._chunks:
            vector = self._vectors[chunk.id]
            magnitude = _magnitude(vector)
            if not magnitude:
                continue
            score = sum(a * b for a, b in zip(query, vector)) / (query_magnitude * magnitude)
            if score > 0:
                results.append(Evidence(chunk=chunk, score=score))
        return sorted(results, key=lambda item: (-item.score, item.chunk.id))[:limit]


class IdentityReranker:
    def rerank(self, question: str, candidates: list[Evidence], limit: int = 6) -> list[Evidence]:
        return candidates[:limit]


class ScoringReranker:
    """Sorts local candidates using a caller-provided relevance scorer."""

    def __init__(self, score: Callable[[str, str], float]):
        self._score = score

    def rerank(self, question: str, candidates: list[Evidence], limit: int = 6) -> list[Evidence]:
        if limit < 1 or not candidates:
            return []
        scored_candidates = []
        for candidate in candidates:
            score = self._score(question, candidate.chunk.text)
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise RerankerError("Local reranker returned an invalid relevance score.")
            scored_candidates.append(Evidence(chunk=candidate.chunk, score=float(score)))
        return sorted(scored_candidates, key=lambda item: (-item.score, item.chunk.id))[:limit]


class MLXLocalReranker:
    """Scores query-document pairs with one pre-provisioned local Qwen reranker model."""

    _INSTRUCTION = "Given a document question, retrieve relevant passages that answer the question"
    _PREFIX = (
        "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the "
        "Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n"
        "<|im_start|>user\n"
    )
    _SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

    def __init__(self, model_directory: Path):
        self._model_directory = model_directory.expanduser().resolve()
        required_files = ("config.json", "model.safetensors", "tokenizer.json")
        if not self._model_directory.is_dir() or any(
            not (self._model_directory / filename).is_file() for filename in required_files
        ):
            raise RerankerError("A local MLX reranker directory with config, tokenizer, and weights is required.")
        self._model = None
        self._tokenizer = None
        self._reranker = ScoringReranker(self._score)

    def rerank(self, question: str, candidates: list[Evidence], limit: int = 6) -> list[Evidence]:
        return self._reranker.rerank(question, candidates, limit)

    def _score(self, question: str, document: str) -> float:
        if not question.strip() or not document.strip():
            raise RerankerError("Local reranker inputs must be non-empty.")
        os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            import mlx.core as mx
            from mlx_lm import load
        except ImportError as exc:
            raise RerankerError("Local reranking requires the optional MLX runtime.") from exc
        try:
            if self._model is None or self._tokenizer is None:
                self._model, self._tokenizer = load(str(self._model_directory))
            tokenizer = getattr(self._tokenizer, "_tokenizer", self._tokenizer)
            yes_token_id = tokenizer.convert_tokens_to_ids("yes")
            no_token_id = tokenizer.convert_tokens_to_ids("no")
            if not isinstance(yes_token_id, int) or not isinstance(no_token_id, int):
                raise RerankerError("The local reranker tokenizer does not provide yes/no token IDs.")
            content = f"<Instruct>: {self._INSTRUCTION}\n<Query>: {question}\n<Document>: {document}"
            token_ids = (
                tokenizer.encode(self._PREFIX, add_special_tokens=False)
                + tokenizer.encode(content, add_special_tokens=False)
                + tokenizer.encode(self._SUFFIX, add_special_tokens=False)
            )
            logits = self._model(mx.array([token_ids]))[:, -1, :]
            binary_logits = mx.stack([logits[0, no_token_id], logits[0, yes_token_id]])
            probability = mx.exp(binary_logits[1] - mx.logsumexp(binary_logits))
            mx.eval(probability)
            return float(probability)
        except RerankerError:
            raise
        except Exception as exc:
            raise RerankerError("Local MLX reranking failed.") from exc


def _terms(text: str) -> set[str]:
    stop_words = {
        "a", "an", "and", "are", "at", "be", "for", "from", "in", "is", "it", "of", "on",
        "or", "the", "to", "was", "what", "where", "who", "with",
    }
    return {
        term.lower()
        for term in re.findall(r"[A-Za-z0-9]+", text)
        if len(term) > 1 and term.lower() not in stop_words
    }


def has_lexical_support(question: str, texts: list[str]) -> bool:
    """Returns whether cited text contains a meaningful term from the question."""
    question_terms = _terms(question)
    if not question_terms:
        return False
    return any(question_terms.intersection(_terms(text)) for text in texts)


def _vector(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for term in _terms(text):
        digest = hashlib.sha256(term.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:8], "big") % dimensions] += 1.0
    return vector


def _magnitude(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))
