from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from hearth.domain import Chunk, Evidence
from hearth.retrieval import MLXLocalReranker


DEFAULT_MODEL_DIRECTORY = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Hearth"
    / "models"
    / "qwen3-reranker-0.6b-4bit"
)


@unittest.skipUnless(
    os.environ.get("HEARTH_RUN_RERANKER_INTEGRATION") == "1",
    "Set HEARTH_RUN_RERANKER_INTEGRATION=1 to run the local MLX reranker integration test.",
)
class RerankerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.model_directory = Path(os.environ.get("HEARTH_RERANKER_MODEL_DIRECTORY", DEFAULT_MODEL_DIRECTORY))
        if not (self.model_directory / "model.safetensors").is_file():
            self.skipTest("A pre-provisioned local reranker model is required.")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_relevant_document_ranks_first(self) -> None:
        candidates = [
            Evidence(Chunk(1, 1, "fixture.md", 1, None, "The review is on Tuesday.", 0, 26, "native", None), 0.0),
            Evidence(
                Chunk(
                    2,
                    1,
                    "fixture.md",
                    1,
                    None,
                    "The archive location is encrypted local storage.",
                    0,
                    48,
                    "native",
                    None,
                ),
                0.0,
            ),
        ]

        ranked = MLXLocalReranker(self.model_directory).rerank("Where is the archive location?", candidates)

        self.assertEqual(ranked[0].chunk.id, 2)
