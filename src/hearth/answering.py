from __future__ import annotations

from .domain import Answer, Citation, Evidence


class EvidenceAnswerer:
    """Renders evidence directly until a constrained local generator is integrated."""

    def answer(self, evidence: list[Evidence]) -> Answer:
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


def validate_answer(answer: Answer, evidence: list[Evidence]) -> Answer:
    """Ensures citations point only to chunks given to the answerer."""
    allowed_ids = {item.chunk.id for item in evidence}
    if answer.status == "abstained":
        return Answer.abstain()
    if not answer.citations or any(citation.chunk_id not in allowed_ids for citation in answer.citations):
        return Answer.abstain()
    return answer
