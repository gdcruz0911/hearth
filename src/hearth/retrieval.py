from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from .domain import Chunk, Evidence


class LocalIndex(Protocol):
    """Local retrieval boundary for the future vector-index implementation."""

    def search(self, question: str, limit: int = 20) -> list[Evidence]: ...


class Reranker(Protocol):
    """Local reranking boundary. Implementations must not send evidence remotely."""

    def rerank(self, question: str, candidates: list[Evidence], limit: int = 6) -> list[Evidence]: ...


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


def _vector(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for term in _terms(text):
        digest = hashlib.sha256(term.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:8], "big") % dimensions] += 1.0
    return vector


def _magnitude(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))
