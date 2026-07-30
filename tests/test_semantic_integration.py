from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from hearth.embedding import FlatVectorIndex, IndexCompatibilityError, MLXEmbedder
from hearth.service import HearthService


DEFAULT_MODEL_DIRECTORY = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Hearth"
    / "models"
    / "qwen3-embedding-0.6b-4bit-dwq"
)


@unittest.skipUnless(
    os.environ.get("HEARTH_RUN_SEMANTIC_INTEGRATION") == "1",
    "Set HEARTH_RUN_SEMANTIC_INTEGRATION=1 to run the local MLX semantic integration test.",
)
class SemanticIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.document = self.root / "operations.md"
        self.model_directory = Path(os.environ.get("HEARTH_EMBEDDING_MODEL_DIRECTORY", DEFAULT_MODEL_DIRECTORY))
        if not (self.model_directory / "model.safetensors").is_file():
            self.skipTest("A pre-provisioned local embedding model is required.")
        semantic_index = FlatVectorIndex(self.root / "semantic-index", MLXEmbedder(self.model_directory))
        self.service = HearthService(self.root / "hearth.sqlite", semantic_index=semantic_index)

    def tearDown(self) -> None:
        self.service.close()
        self.temporary_directory.cleanup()

    def test_reindex_deletion_and_manifest_mismatch_fail_safely(self) -> None:
        self.document.write_text("The archive location is the north vault.", encoding="utf-8")
        self.service.import_document(str(self.document))

        initial_answer = self.service.answer("Where is the archive location?")

        self.document.write_text("The archive location is the south vault.", encoding="utf-8")
        self.service.reindex_document(str(self.document))
        updated_answer = self.service.answer("Where is the archive location?")

        self.assertEqual(initial_answer.status, "supported")
        self.assertIn("north vault", initial_answer.text)
        self.assertEqual(updated_answer.status, "supported")
        self.assertIn("south vault", updated_answer.text)
        self.assertNotIn("north vault", updated_answer.text)

        self.assertTrue(self.service.remove_document(str(self.document)))
        self.assertEqual(self.service.answer("Where is the archive location?").status, "abstained")

        self.service.import_document(str(self.document))
        active_payload = json.loads((self.root / "semantic-index" / "active.json").read_text(encoding="utf-8"))
        active_version = active_payload["version"]
        manifest_path = self.root / "semantic-index" / "versions" / active_version / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["model_name"] = "incompatible-test-model"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(IndexCompatibilityError, "does not match"):
            self.service.answer("Where is the archive location?")
