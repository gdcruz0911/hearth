from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hearth.runtime import RuntimeProfile, RuntimeProfileError, load_runtime_profile, write_runtime_profile


class RuntimeProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_load_resolves_relative_paths_from_the_profile_directory(self) -> None:
        profile_path = self.root / "profiles" / "hearth.json"
        profile_path.parent.mkdir()
        profile_path.write_text(
            json.dumps(
                {
                    "format": "hearth-runtime-profile-v1",
                    "database": "runtime/hearth.sqlite",
                    "embedding_model": "models/embedder",
                    "index_directory": "runtime/index",
                    "relationship_minimum_score": 0.81,
                    "source_roots": ["Desktop", "Documents"],
                }
            ),
            encoding="utf-8",
        )

        profile = load_runtime_profile(profile_path)

        self.assertEqual(profile.database, (profile_path.parent / "runtime/hearth.sqlite").resolve())
        self.assertEqual(profile.embedding_model, (profile_path.parent / "models/embedder").resolve())
        self.assertEqual(profile.index_directory, (profile_path.parent / "runtime/index").resolve())
        self.assertEqual(profile.relationship_minimum_score, 0.81)
        self.assertEqual(
            profile.source_roots,
            ((profile_path.parent / "Desktop").resolve(), (profile_path.parent / "Documents").resolve()),
        )

    def test_recall_roots_round_trip_and_default_to_none(self) -> None:
        notes = self.root / "notes"
        written = write_runtime_profile(self.root / "with.json", RuntimeProfile(database=self.root / "h.sqlite", recall_roots=(notes,)))
        plain = write_runtime_profile(self.root / "without.json", RuntimeProfile(database=self.root / "h.sqlite"))

        self.assertEqual(load_runtime_profile(written).recall_roots, (notes.resolve(),))
        self.assertEqual(load_runtime_profile(plain).recall_roots, ())

    def test_write_refuses_to_overwrite_an_existing_profile(self) -> None:
        profile_path = self.root / "hearth.json"
        write_runtime_profile(profile_path, RuntimeProfile(database=self.root / "hearth.sqlite"))

        with self.assertRaisesRegex(RuntimeProfileError, "overwrite"):
            write_runtime_profile(profile_path, RuntimeProfile(database=self.root / "other.sqlite"))

    def test_load_rejects_the_retired_generator_model_setting(self) -> None:
        profile_path = self.root / "hearth.json"
        profile_path.write_text(
            json.dumps({"format": "hearth-runtime-profile-v1", "database": "hearth.sqlite", "generator_model": "models/gen"}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeProfileError, "unsupported settings"):
            load_runtime_profile(profile_path)
