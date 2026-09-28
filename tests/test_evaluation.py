from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hearth.evaluation import (
    record_question_set,
    EvaluationCorpusError,
    evaluate_corpus,
    load_evaluation_corpus,
)
from hearth.service import HearthService


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

    def test_scaffold_cites_related_evidence_for_an_unanswerable_question(self) -> None:
        self.service.import_document(str(self.document))

        answer = self.service.answer("What is the deployment owner's phone number?")

        self.assertEqual(answer.status, "supported")
        self.assertIn("Ada", answer.citations[0].quote)


if __name__ == "__main__":
    unittest.main()


class QuestionSetRecordTests(unittest.TestCase):
    def test_every_stage_is_recorded_and_only_supported_and_unsupported_cases_are_scored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "operations.md").write_text("# Operations\n\nThe deployment owner is Ada.\n", encoding="utf-8")
            service = HearthService(root / "hearth.sqlite")
            self.addCleanup(service.close)
            service.import_document(str(root / "operations.md"))
            question_set = root / "questions.json"
            question_set.write_text(json.dumps({"cases": [
                {"id": "owner", "question": "Who is the deployment owner?", "label": "supported", "quote_contains": "Ada"},
                {"id": "none", "question": "Which zebra won?", "label": "unsupported"},
                {"id": "vague", "question": "Who owns it?", "label": "ambiguous question"},
                {"id": "premise", "question": "Why did Ada resign?", "label": "contradicted premise"},
                {"id": "held", "question": "Who is the deployment owner?", "label": "supported", "quote_contains": "Ada", "excluded": True},
            ]}), encoding="utf-8")

            records = {record["id"]: record for record in record_question_set(service, question_set)}
            answer = service.answer("Who is the deployment owner?")

        self.assertEqual({key: record["outcome"] for key, record in records.items()},
                         {"owner": "success", "none": "success", "vague": "not scored", "premise": "not scored", "held": "not scored"})
        owner = records["owner"]
        self.assertTrue(owner["keyword"] and owner["fused"] and owner["reranked"])
        self.assertEqual((owner["status"], owner["lexical_support"]), ("supported", True))
        self.assertEqual(owner["citations"], [citation.chunk_id for citation in answer.citations])
        self.assertEqual(records["none"]["status"], "abstained")
        self.assertNotIn("Ada", json.dumps(owner["reranked"]))

    def test_answers_cite_six_chunks_while_the_record_keeps_every_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = HearthService(root / "hearth.sqlite")
            self.addCleanup(service.close)
            for number in range(8):
                note = root / f"team-{number}.md"
                note.write_text(f"The deployment owner for team {number} is person {number}.", encoding="utf-8")
                service.import_document(str(note))

            answer, record = service.trace("Who is the deployment owner?")

        self.assertEqual((len(answer.citations), len(record["citations"])), (6, 6))
        self.assertEqual((len(record["fused"]), len(record["reranked"])), (8, 8))

    def test_a_case_without_one_of_the_five_labels_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            question_set = Path(directory) / "questions.json"
            question_set.write_text(json.dumps({"cases": [{"id": "x", "question": "Who?", "label": "unanswerable"}]}), encoding="utf-8")
            service = HearthService(Path(directory) / "hearth.sqlite")
            self.addCleanup(service.close)

            with self.assertRaisesRegex(EvaluationCorpusError, "needs a label"):
                record_question_set(service, question_set)

