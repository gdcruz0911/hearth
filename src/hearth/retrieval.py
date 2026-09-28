from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Callable, Protocol

from .domain import Evidence
from .embedding import MLX_LOCK, _sha256_file

STOP_WORDS = frozenset({
    "a", "an", "and", "are", "at", "be", "for", "from", "in", "is", "it", "of", "on",
    "or", "the", "to", "was", "what", "where", "who", "with",
})
RRF_K = 60  # The published default for reciprocal rank fusion.


class LocalIndex(Protocol):
    """Local retrieval boundary for the future vector-index implementation."""

    def search(self, question: str, limit: int = 20) -> list[Evidence]: ...


class Reranker(Protocol):
    """Local reranking boundary. Implementations must not send evidence remotely."""

    def rerank(self, question: str, candidates: list[Evidence], limit: int = 6) -> list[Evidence]: ...


class RerankerError(RuntimeError):
    """Raised when local reranking cannot safely score an evidence candidate."""


def reciprocal_rank_fusion(rankings: list[list[Evidence]], limit: int = 20, k: int = RRF_K) -> list[Evidence]:
    """Merge ranked lists by rank alone, so scores on different scales need no calibration.

    Each chunk scores the sum of 1 / (k + rank) over the lists it appears in; k = 60 is the published default.
    """
    scores: dict[int, float] = {}
    chunks = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item.chunk.id] = scores.get(item.chunk.id, 0.0) + 1 / (k + rank)
            chunks[item.chunk.id] = item.chunk
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    return [Evidence(chunk=chunks[chunk_id], score=scores[chunk_id]) for chunk_id in ordered[:limit]]


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

    def describe(self) -> dict[str, object]:
        """Which reranker model scored the candidates, by directory name and weights hash."""
        return {"name": type(self).__name__, "model": self._model_directory.name,
                "weights_sha256": _sha256_file(self._model_directory / "model.safetensors")}

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
            with MLX_LOCK:
                if self._model is None or self._tokenizer is None:
                    # MLX keeps freed GPU buffers for reuse; with chunks of every length that cache grew past 10 GB.
                    mx.set_cache_limit(256 * 1024 * 1024)
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
            with MLX_LOCK:  # Shared with the embedder: see MLX_LOCK.
                logits = self._model(mx.array([token_ids]))[:, -1, :]
                binary_logits = mx.stack([logits[0, no_token_id], logits[0, yes_token_id]])
                probability = mx.exp(binary_logits[1] - mx.logsumexp(binary_logits))
                mx.eval(probability)
                return float(probability)
        except RerankerError:
            raise
        except Exception as exc:
            raise RerankerError("Local MLX reranking failed.") from exc


def terms(text: str) -> set[str]:
    return {
        term.lower()
        for term in re.findall(r"[A-Za-z0-9]+", text)
        if len(term) > 1 and term.lower() not in STOP_WORDS
    }


def has_lexical_support(question: str, texts: list[str]) -> bool:
    """Returns whether cited text contains a meaningful term from the question."""
    question_terms = terms(question)
    if not question_terms:
        return False
    return any(question_terms.intersection(terms(text)) for text in texts)
