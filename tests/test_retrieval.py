from __future__ import annotations

import math
import unittest

from hearth.domain import Chunk, Evidence
from hearth.retrieval import RerankerError, ScoringReranker, reciprocal_rank_fusion


def _evidence(chunk_id: int, text: str) -> Evidence:
    return Evidence(Chunk(chunk_id, 1, "fixture.md", 1, None, text, 0, len(text), "native", None), 0.0)


class ReciprocalRankFusionTests(unittest.TestCase):
    def test_a_chunk_both_lists_rank_beats_one_that_tops_a_single_list(self) -> None:
        keyword = [_evidence(1, "only keyword"), _evidence(2, "both")]
        semantic = [_evidence(3, "only semantic"), _evidence(2, "both")]

        fused = reciprocal_rank_fusion([keyword, semantic])

        self.assertEqual([item.chunk.id for item in fused], [2, 1, 3])
        self.assertAlmostEqual(fused[0].score, 2 / 62)
        self.assertEqual(reciprocal_rank_fusion([keyword, semantic], limit=1)[0].chunk.id, 2)


class ScoringRerankerTests(unittest.TestCase):
    def test_sorts_local_scores_and_breaks_ties_by_chunk_id(self) -> None:
        scores = {"first": 0.7, "second": 0.9, "third": 0.9}
        reranker = ScoringReranker(lambda question, document: scores[document])

        ranked = reranker.rerank("query", [_evidence(3, "third"), _evidence(2, "second"), _evidence(1, "first")])

        self.assertEqual([item.chunk.id for item in ranked], [2, 3, 1])
        self.assertEqual([item.score for item in ranked], [0.9, 0.9, 0.7])

    def test_respects_limit_and_rejects_invalid_scores(self) -> None:
        reranker = ScoringReranker(lambda question, document: 0.5)

        self.assertEqual(len(reranker.rerank("query", [_evidence(1, "first"), _evidence(2, "second")], limit=1)), 1)
        self.assertEqual(reranker.rerank("query", [_evidence(1, "first")], limit=0), [])

        for invalid_score in (True, math.nan, math.inf, "0.5"):
            with self.subTest(invalid_score=invalid_score):
                invalid_reranker = ScoringReranker(lambda question, document: invalid_score)
                with self.assertRaisesRegex(RerankerError, "invalid relevance score"):
                    invalid_reranker.rerank("query", [_evidence(1, "first")])
