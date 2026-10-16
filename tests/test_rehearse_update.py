from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import rehearse_update


class RehearsalRefTests(unittest.TestCase):
    def test_release_refs_resolve_to_commits_before_the_rehearsal_compares_them(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)

            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()

            git("init", "-q", "-b", "main")
            identity = ("-c", "user.name=Person", "-c", "user.email=person@example.com")
            git(*identity, "commit", "-q", "--allow-empty", "-m", "initial")
            git(*identity, "tag", "-a", "v1.0.0", "-m", "release")
            commit = git("rev-parse", "HEAD")
            self.assertNotEqual(git("rev-parse", "v1.0.0"), commit)  # An annotated tag has its own object ID.
            profile = repo / "profile.json"
            profile.write_text(json.dumps({"database": "database", "embedding_model": "model", "index_directory": "index"}), encoding="utf-8")

            with mock.patch.object(rehearse_update, "REPO", repo), mock.patch.object(rehearse_update, "REAL_PROFILE", profile), \
                    mock.patch.object(rehearse_update.Rehearsal, "real_state", return_value=()):
                for ref in ("v1.0.0", "main", commit):
                    with self.subTest(ref=ref):
                        self.assertEqual(rehearse_update.Rehearsal(ref).ref, commit)
                for ref in ("missing-release", "HEAD^{tree}"):
                    with self.subTest(ref=ref), self.assertRaises(subprocess.CalledProcessError):
                        rehearse_update.Rehearsal(ref)
