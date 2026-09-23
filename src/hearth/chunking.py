from __future__ import annotations

import re

from .domain import ExtractedPage

CHUNKING_VERSION = "structure-aware-v2-target-400-overlap-60"

# A unit is a Markdown heading line, or text up to a sentence end, a blank line,
# or the start of the next heading or list item. Markdown carries no terminal
# punctuation, so sentence boundaries alone leave a whole list as one unit.
_UNIT = re.compile(
    r"^\#{1,6}\s[^\n]*"
    r"|\S[\s\S]*?(?:[.!?](?=\s|$)|(?=\n\s*\n)|(?=\n\s*(?:\#{1,6}\s|[-*+]\s|\d+[.)]\s))|$)",
    re.MULTILINE,
)
_WORD = re.compile(r"\S+")

Span = tuple[str, int, int]


def chunk_page(page: ExtractedPage, target_words: int = 400, overlap_words: int = 60) -> list[Span]:
    """Chunk one page on structural and sentence boundaries.

    Sizes count whitespace-separated words, not model tokens. A Qwen3 tokenizer
    produces roughly 1.3 to 1.5 tokens per English word, so the 400 default is
    about 550 model tokens. Chunking is deliberately not coupled to a tokenizer
    (ADR-0018) so that changing the embedding model does not force a full
    re-extraction of every document.
    """
    if target_words < 1 or overlap_words < 0 or overlap_words >= target_words:
        raise ValueError("target_words must be positive and exceed overlap_words.")

    chunks: list[Span] = []
    current: list[Span] = []
    current_words = 0

    for unit in _units(page.text, target_words):
        unit_words = len(unit[0].split())
        if current and current_words + unit_words > target_words:
            chunks.append(_join(current))
            current = _overlap(current, overlap_words)
            current_words = sum(len(item[0].split()) for item in current)
        current.append(unit)
        current_words += unit_words
    if current:
        chunks.append(_join(current))
    return chunks


def _units(text: str, target_words: int) -> list[Span]:
    units: list[Span] = []
    for match in _UNIT.finditer(text):
        units.extend(_bounded(match.group(), match.start(), target_words))
    return units


def _bounded(text: str, offset: int, target_words: int) -> list[Span]:
    """Split one unit no boundary broke, so no input can produce an unbounded chunk."""
    words = list(_WORD.finditer(text))
    if not words:
        return []
    step = min(target_words, len(words))
    spans: list[Span] = []
    for index in range(0, len(words), step):
        group = words[index : index + step]
        start, end = group[0].start(), group[-1].end()
        spans.append((text[start:end], offset + start, offset + end))
    return spans


def _join(units: list[Span]) -> Span:
    return (" ".join(unit[0].strip() for unit in units), units[0][1], units[-1][2])


def _overlap(units: list[Span], overlap_words: int) -> list[Span]:
    """Carry back at most overlap_words. A unit larger than that carries nothing."""
    retained: list[Span] = []
    count = 0
    for unit in reversed(units):
        words = len(unit[0].split())
        if count + words > overlap_words:
            break
        retained.insert(0, unit)
        count += words
    return retained
