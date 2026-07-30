from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hearth.answering import LocalInferenceError, MLXLocalGenerator, StructuredGeneratorAnswerer
from hearth.domain import Chunk, Evidence


class FakeGenerator:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class StructuredGeneratorAnswererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = [
            Evidence(
                Chunk(11, 1, "operations.md", 1, "Operations", "The owner is Ada.", 0, 17, "native", None),
                0.9,
            ),
            Evidence(
                Chunk(12, 1, "operations.md", 1, "Operations", "The review is Tuesday.", 18, 41, "native", None),
                0.8,
            ),
        ]

    def test_resolves_citations_from_approved_evidence(self) -> None:
        generator = FakeGenerator('{"status":"supported","answer":"The owner is Ada.","citation_chunk_ids":[11]}')

        answer = StructuredGeneratorAnswerer(generator).answer("Who is the owner?", self.evidence)

        self.assertEqual(answer.status, "supported")
        self.assertEqual(answer.text, "The owner is Ada.")
        self.assertEqual(answer.citations[0].chunk_id, 11)
        self.assertEqual(answer.citations[0].quote, "The owner is Ada.")
        self.assertIn("Who is the owner?", generator.prompts[0])
        self.assertIn('"chunk_id": 11', generator.prompts[0])
        self.assertIn("verbatim contiguous excerpt", generator.prompts[0])

    def test_normalized_verbatim_answer_is_supported(self) -> None:
        generator = FakeGenerator('{"status":"supported","answer":"the owner is ada.","citation_chunk_ids":[11]}')

        answer = StructuredGeneratorAnswerer(generator).answer("Who is the owner?", self.evidence)

        self.assertEqual(answer.status, "supported")
        self.assertEqual(answer.text, "the owner is ada.")

    def test_verbatim_partial_answer_is_supported(self) -> None:
        generator = FakeGenerator('{"status":"supported","answer":"Ada","citation_chunk_ids":[11]}')

        answer = StructuredGeneratorAnswerer(generator).answer("Who is the owner?", self.evidence)

        self.assertEqual(answer.status, "supported")
        self.assertEqual(answer.text, "Ada")

    def test_paraphrased_answer_abstains_even_with_valid_citation(self) -> None:
        generator = FakeGenerator('{"status":"supported","answer":"Ada leads the team.","citation_chunk_ids":[11]}')

        answer = StructuredGeneratorAnswerer(generator).answer("Who is the owner?", self.evidence)

        self.assertEqual(answer.status, "abstained")
        self.assertEqual(answer.citations, ())

    def test_answer_from_an_uncited_chunk_abstains(self) -> None:
        generator = FakeGenerator(
            '{"status":"supported","answer":"The review is Tuesday.","citation_chunk_ids":[11]}'
        )

        answer = StructuredGeneratorAnswerer(generator).answer("When is the review?", self.evidence)

        self.assertEqual(answer.status, "abstained")
        self.assertEqual(answer.citations, ())

    def test_explicit_abstention_is_preserved(self) -> None:
        generator = FakeGenerator('{"status":"abstained","answer":"","citation_chunk_ids":[]}')

        answer = StructuredGeneratorAnswerer(generator).answer("What is the budget?", self.evidence)

        self.assertEqual(answer.status, "abstained")
        self.assertEqual(answer.citations, ())

    def test_invalid_generator_responses_abstain(self) -> None:
        invalid_responses = (
            "not json",
            '{"status":"supported","answer":"Ada","citation_chunk_ids":[99]}',
            '{"status":"supported","answer":"Ada","citation_chunk_ids":[11,11]}',
            '{"status":"supported","answer":"Ada","citation_chunk_ids":[true]}',
            '{"status":"supported","answer":"Ada","citation_chunk_ids":"11"}',
            '{"status":"supported","answer":"Ada","citation_chunk_ids":[]}',
            '{"status":"supported","answer":[],"citation_chunk_ids":[11]}',
            '{"status":"supported","answer":"Ada","citation_chunk_ids":[11],"extra":true}',
            '{"status":"supported","status":"abstained","answer":"Ada","citation_chunk_ids":[11]}',
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                answer = StructuredGeneratorAnswerer(FakeGenerator(response)).answer("Who is the owner?", self.evidence)
                self.assertEqual(answer.status, "abstained")
                self.assertEqual(answer.citations, ())

    def test_local_generator_requires_local_weight_file(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaises(LocalInferenceError):
                MLXLocalGenerator(Path(directory))
