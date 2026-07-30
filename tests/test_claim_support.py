from __future__ import annotations

import unittest

from hearth.claim_support import StructuredClaimSupportChecker


class FakeGenerator:
    def __init__(self, response: str):
        self._response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response


class StructuredClaimSupportCheckerTests(unittest.TestCase):
    def test_accepts_only_strict_supported_response(self) -> None:
        generator = FakeGenerator('{"status":"supported"}')

        supported = StructuredClaimSupportChecker(generator).supports(
            "Who is the owner?", "The owner is Ada.", ("The owner is Ada.",)
        )

        self.assertTrue(supported)
        self.assertIn("every factual detail, qualifier, relationship, and time constraint", generator.prompts[0])

    def test_fails_closed_for_unsupported_or_malformed_response(self) -> None:
        for response in (
            '{"status":"unsupported"}',
            '{"status":"supported","reason":"because"}',
            '{"status":"maybe"}',
            "not json",
        ):
            with self.subTest(response=response):
                self.assertFalse(
                    StructuredClaimSupportChecker(FakeGenerator(response)).supports(
                        "Who is the owner?", "The owner is Ada.", ("The owner is Ada.",)
                    )
                )
