from __future__ import annotations

import re

from .domain import ExtractedPage


def chunk_page(page: ExtractedPage, target_tokens: int = 400, overlap_tokens: int = 60) -> list[tuple[str, int, int]]:
    """Chunk one page on sentence boundaries using whitespace as a token estimate."""
    if target_tokens < 1 or overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("target_tokens must be positive and exceed overlap_tokens.")

    sentences = [match for match in re.finditer(r"\S[\s\S]*?(?:[.!?](?=\s|$)|$)", page.text) if match.group().strip()]
    chunks: list[tuple[str, int, int]] = []
    current: list[re.Match[str]] = []
    current_tokens = 0

    for sentence in sentences:
        sentence_tokens = len(sentence.group().split())
        if current and current_tokens + sentence_tokens > target_tokens:
            chunks.append(_make_chunk(current))
            current = _overlap_sentences(current, overlap_tokens)
            current_tokens = sum(len(item.group().split()) for item in current)
        current.append(sentence)
        current_tokens += sentence_tokens
    if current:
        chunks.append(_make_chunk(current))
    return chunks


def _make_chunk(sentences: list[re.Match[str]]) -> tuple[str, int, int]:
    start = sentences[0].start()
    end = sentences[-1].end()
    return (" ".join(sentence.group().strip() for sentence in sentences), start, end)


def _overlap_sentences(sentences: list[re.Match[str]], overlap_tokens: int) -> list[re.Match[str]]:
    retained: list[re.Match[str]] = []
    token_count = 0
    for sentence in reversed(sentences):
        retained.insert(0, sentence)
        token_count += len(sentence.group().split())
        if token_count >= overlap_tokens:
            break
    return retained
