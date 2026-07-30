from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .claim_support import ClaimSupportChecker
from .service import HearthService


class EvaluationCorpusError(ValueError):
    """Raised when an evaluation corpus does not follow the local schema."""


@dataclass(frozen=True)
class ExpectedCitation:
    document_name: str
    page_number: int
    quote_contains: str


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    question: str
    expected_status: str
    expected_citations: tuple[ExpectedCitation, ...]


@dataclass(frozen=True)
class EvaluationCorpus:
    document_paths: tuple[Path, ...]
    cases: tuple[EvaluationCase, ...]


@dataclass(frozen=True)
class ClaimSupportCase:
    id: str
    question: str
    claim: str
    evidence: tuple[str, ...]
    expected_supported: bool


@dataclass(frozen=True)
class ClaimSupportCorpus:
    cases: tuple[ClaimSupportCase, ...]


@dataclass(frozen=True)
class EvaluationOutcome:
    case_id: str
    passed: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ClaimSupportOutcome:
    case_id: str
    passed: bool
    expected_supported: bool
    received_supported: bool


def load_evaluation_corpus(path: Path) -> EvaluationCorpus:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationCorpusError("Evaluation corpus file does not exist.") from exc
    except UnicodeDecodeError as exc:
        raise EvaluationCorpusError("Evaluation corpus must be UTF-8 encoded.") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationCorpusError("Evaluation corpus is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise EvaluationCorpusError("Evaluation corpus must be a JSON object.")
    document_names = _required_string_list(payload, "documents")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvaluationCorpusError("Evaluation corpus requires at least one case.")
    cases = tuple(_parse_case(item, index) for index, item in enumerate(raw_cases, start=1))
    case_ids = [case.id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise EvaluationCorpusError("Evaluation case IDs must be unique.")
    return EvaluationCorpus(
        document_paths=tuple((path.parent / document_name).resolve() for document_name in document_names),
        cases=cases,
    )


def evaluate_corpus(service: HearthService, corpus: EvaluationCorpus) -> tuple[EvaluationOutcome, ...]:
    for document_path in corpus.document_paths:
        service.import_document(str(document_path))
    return tuple(_evaluate_case(service, case) for case in corpus.cases)


def load_claim_support_corpus(path: Path) -> ClaimSupportCorpus:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationCorpusError("Claim-support corpus file does not exist.") from exc
    except UnicodeDecodeError as exc:
        raise EvaluationCorpusError("Claim-support corpus must be UTF-8 encoded.") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationCorpusError("Claim-support corpus is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise EvaluationCorpusError("Claim-support corpus must be a JSON object.")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvaluationCorpusError("Claim-support corpus requires at least one case.")
    cases = tuple(_parse_claim_support_case(item, index) for index, item in enumerate(raw_cases, start=1))
    case_ids = [case.id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise EvaluationCorpusError("Claim-support case IDs must be unique.")
    return ClaimSupportCorpus(cases)


def evaluate_claim_support_corpus(
    checker: ClaimSupportChecker, corpus: ClaimSupportCorpus
) -> tuple[ClaimSupportOutcome, ...]:
    outcomes = []
    for case in corpus.cases:
        received_supported = checker.supports(case.question, case.claim, case.evidence)
        outcomes.append(
            ClaimSupportOutcome(
                case_id=case.id,
                passed=received_supported == case.expected_supported,
                expected_supported=case.expected_supported,
                received_supported=received_supported,
            )
        )
    return tuple(outcomes)


def _parse_case(value: object, index: int) -> EvaluationCase:
    if not isinstance(value, dict):
        raise EvaluationCorpusError(f"Evaluation case {index} must be a JSON object.")
    case_id = _required_string(value, "id", index)
    question = _required_string(value, "question", index)
    expected_status = _required_string(value, "expected_status", index)
    if expected_status not in {"supported", "abstained"}:
        raise EvaluationCorpusError(f"Evaluation case {index} has an unsupported expected_status.")
    raw_citations = value.get("expected_citations", [])
    if not isinstance(raw_citations, list):
        raise EvaluationCorpusError(f"Evaluation case {index} expected_citations must be a list.")
    citations = tuple(_parse_expected_citation(item, index) for item in raw_citations)
    if expected_status == "supported" and not citations:
        raise EvaluationCorpusError(f"Supported evaluation case {index} requires an expected citation.")
    if expected_status == "abstained" and citations:
        raise EvaluationCorpusError(f"Abstained evaluation case {index} cannot define expected citations.")
    return EvaluationCase(case_id, question, expected_status, citations)


def _parse_claim_support_case(value: object, index: int) -> ClaimSupportCase:
    if not isinstance(value, dict):
        raise EvaluationCorpusError(f"Claim-support case {index} must be a JSON object.")
    case_id = _required_string(value, "id", index)
    question = _required_string(value, "question", index)
    claim = _required_string(value, "claim", index)
    evidence = _required_string_list(value, "evidence")
    expected_supported = value.get("expected_supported")
    if not isinstance(expected_supported, bool):
        raise EvaluationCorpusError(f"Claim-support case {index} expected_supported must be a boolean.")
    return ClaimSupportCase(case_id, question, claim, tuple(evidence), expected_supported)


def _parse_expected_citation(value: object, case_index: int) -> ExpectedCitation:
    if not isinstance(value, dict):
        raise EvaluationCorpusError(f"Evaluation case {case_index} has an invalid expected citation.")
    document_name = _required_string(value, "document_name", case_index)
    quote_contains = _required_string(value, "quote_contains", case_index)
    page_number = value.get("page_number")
    if not isinstance(page_number, int) or page_number < 1:
        raise EvaluationCorpusError(f"Evaluation case {case_index} expected citation page_number must be positive.")
    return ExpectedCitation(document_name, page_number, quote_contains)


def _required_string_list(payload: dict[object, object], field: str) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise EvaluationCorpusError(f"Evaluation corpus {field} must be a non-empty list of strings.")
    return value


def _required_string(payload: dict[object, object], field: str, case_index: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise EvaluationCorpusError(f"Evaluation case {case_index} {field} must be a non-empty string.")
    return value


def _evaluate_case(service: HearthService, case: EvaluationCase) -> EvaluationOutcome:
    answer = service.answer(case.question)
    errors = []
    if answer.status != case.expected_status:
        errors.append(f"expected status {case.expected_status}, received {answer.status}")
    if case.expected_status == "abstained" and answer.citations:
        errors.append("abstained answer included citations")
    for expected in case.expected_citations:
        if not any(
            citation.document_name == expected.document_name
            and citation.page_number == expected.page_number
            and expected.quote_contains in citation.quote
            for citation in answer.citations
        ):
            errors.append(f"missing expected citation for {expected.document_name} page {expected.page_number}")
    return EvaluationOutcome(case_id=case.id, passed=not errors, errors=tuple(errors))
