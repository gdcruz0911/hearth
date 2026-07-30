from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hearth.evaluation import (
    EvaluationCorpusError,
    evaluate_claim_support_corpus,
    evaluate_corpus,
    load_claim_support_corpus,
    load_evaluation_corpus,
)
from hearth.service import HearthService


class FakeClaimSupportChecker:
    def __init__(self, outcomes: dict[str, bool]):
        self._outcomes = outcomes

    def supports(self, question: str, claim: str, evidence: tuple[str, ...]) -> bool:
        return self._outcomes[claim]


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "hearth.sqlite"
        self.document = self.root / "operations.md"
        self.document.write_text("# Operations\n\nThe deployment owner is Ada.\n", encoding="utf-8")
        self.corpus_path = self.root / "corpus.json"
        self.service = HearthService(self.database)

    def tearDown(self) -> None:
        self.service.close()
        self.temporary_directory.cleanup()

    def test_evaluation_reports_supported_citation_and_abstention(self) -> None:
        self.corpus_path.write_text(
            json.dumps(
                {
                    "documents": [self.document.name],
                    "cases": [
                        {
                            "id": "owner",
                            "question": "Who is the deployment owner?",
                            "expected_status": "supported",
                            "expected_citations": [
                                {"document_name": self.document.name, "page_number": 1, "quote_contains": "Ada"}
                            ],
                        },
                        {
                            "id": "budget",
                            "question": "What is the annual budget?",
                            "expected_status": "abstained",
                            "expected_citations": [],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        outcomes = evaluate_corpus(self.service, load_evaluation_corpus(self.corpus_path))

        self.assertEqual([outcome.case_id for outcome in outcomes], ["owner", "budget"])
        self.assertTrue(all(outcome.passed for outcome in outcomes))

    def test_supported_case_requires_expected_citation(self) -> None:
        self.corpus_path.write_text(
            json.dumps(
                {
                    "documents": [self.document.name],
                    "cases": [{"id": "owner", "question": "Who is the owner?", "expected_status": "supported"}],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(EvaluationCorpusError, "requires an expected citation"):
            load_evaluation_corpus(self.corpus_path)

    def test_public_generator_corpus_has_related_unsupported_case(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures" / "public"

        corpus = load_evaluation_corpus(fixture_root / "generator-evaluation.json")

        self.assertEqual(
            [case.id for case in corpus.cases],
            [
                "supported-deployment-owner",
                "supported-archive-location",
                "abstained-owner-phone-number",
                "abstained-owner-review-date",
            ],
        )
        self.assertEqual(corpus.cases[2].expected_status, "abstained")

    def test_claim_support_corpus_covers_adversarial_cases(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures" / "public"
        corpus = load_claim_support_corpus(fixture_root / "claim-support-evaluation.json")
        checker = FakeClaimSupportChecker(
            {case.claim: case.expected_supported for case in corpus.cases}
        )

        outcomes = evaluate_claim_support_corpus(checker, corpus)

        self.assertEqual(
            [case.id for case in corpus.cases],
            [
                "supported-exact-owner",
                "unsupported-near-match-extra-duty",
                "unsupported-missing-qualifier",
                "unsupported-cross-document-qualifier",
            ],
        )
        self.assertTrue(all(outcome.passed for outcome in outcomes))

    def test_public_semantic_corpus_covers_multiple_documents_and_disambiguation(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures" / "public"
        corpus = load_evaluation_corpus(fixture_root / "semantic-evaluation.json")

        outcomes = evaluate_corpus(self.service, corpus)

        self.assertEqual(
            [outcome.case_id for outcome in outcomes],
            [
                "supported-deployment-owner",
                "supported-archive-location",
                "supported-procurement-owner",
                "supported-training-budget",
                "abstained-quarterly-revenue",
            ],
        )
        self.assertTrue(all(outcome.passed for outcome in outcomes))

    def test_related_unsupported_generator_question_retrieves_scaffold_evidence(self) -> None:
        self.service.import_document(str(self.document))

        answer = self.service.answer("What is the deployment owner's phone number?")

        self.assertEqual(answer.status, "supported")
        self.assertIn("Ada", answer.citations[0].quote)


if __name__ == "__main__":
    unittest.main()
