from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .service import HearthService


class EvaluationCorpusError(ValueError):
    """Raised when an evaluation corpus does not follow the local schema."""


# Every case carries one of these labels. Only the first two are scored; the rest are recorded but never
# counted as successes or failures, so contradicted premises and conflicting evidence stay unevaluated.
LABELS = ("supported", "unsupported", "contradicted premise", "conflicting evidence", "ambiguous question")
SCORED_LABELS = ("supported", "unsupported")


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
class EvaluationOutcome:
    case_id: str
    passed: bool
    errors: tuple[str, ...]


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


def record_question_set(service: HearthService, path: Path) -> list[dict[str, object]]:
    """Run each question of a labeled set against the existing collection and return one full record per question.

    Nothing is imported. A supported case succeeds when a citation contains its expected phrase, an unsupported
    case when Hearth abstains; excluded cases and every other label are recorded as not scored.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationCorpusError("The question set is missing or is not valid UTF-8 JSON.") from exc
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        raise EvaluationCorpusError("The question set requires at least one case.")
    run = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "code": _code_revision(),
        "question_set": {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
        **service.run_description(),
    }
    records = []
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict) or case.get("label") not in LABELS:
            raise EvaluationCorpusError(f"Question {index} needs a label, one of: {', '.join(LABELS)}.")
        question = _required_string(case, "question", index)
        case_id = _required_string(case, "id", index)
        quote = _required_string(case, "quote_contains", index) if case["label"] == "supported" else None
        answer, trace = service.trace(question)
        if case.get("excluded") or case["label"] not in SCORED_LABELS:
            outcome = "not scored"
        elif quote is not None:
            outcome = "success" if any(quote in citation.quote for citation in answer.citations) else "failure"
        else:
            outcome = "success" if answer.status == "abstained" else "failure"
        records.append({"id": case_id, "label": case["label"], "excluded": bool(case.get("excluded")),
                        "expected_quote": quote, "outcome": outcome, **trace, "run": run})
    return records


def _code_revision() -> dict[str, object]:
    """The Git commit Hearth ran from, and whether its source had uncommitted changes; unknown outside a checkout."""
    root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        changes = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no", "--", "src"],
                                 capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "source_modified": None}
    return {"commit": commit, "source_modified": bool(changes)}
