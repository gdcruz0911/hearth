from __future__ import annotations

from .domain import Answer, Citation, Evidence


def answer_from_evidence(evidence: list[Evidence]) -> Answer:
    """Render retrieved evidence verbatim, citing exactly the chunks given, or abstain."""
    if not evidence:
        return Answer.abstain()
    citations = tuple(
        Citation(
            document_name=item.chunk.document_name,
            page_number=item.chunk.page_number,
            section=item.chunk.section,
            chunk_id=item.chunk.id,
            quote=item.chunk.text,
            extraction_method=item.chunk.extraction_method,
            ocr_confidence=item.chunk.ocr_confidence,
        )
        for item in evidence
    )
    return Answer(status="supported", text=evidence[0].chunk.text, citations=citations)
