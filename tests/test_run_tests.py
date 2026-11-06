from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_tests.py"


class RunTestsGuardTests(unittest.TestCase):
    def run_suite(self, source: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "test_scratch.py").write_text(textwrap.dedent(source), encoding="utf-8")
            return subprocess.run([sys.executable, str(SCRIPT), folder], capture_output=True, text=True, timeout=60)

    def test_a_full_run_passes_and_names_the_checkout(self) -> None:
        shown = self.run_suite("""
            import unittest
            class Full(unittest.TestCase):
                def test_one(self): pass
                @unittest.skip("a skip still counts as run")
                def test_two(self): pass
        """)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn(f"Ran 2 of 2 discovered tests in {SCRIPT.parents[1]} at ", shown.stderr)

    def test_a_test_that_ends_the_process_with_exit_code_0_fails_the_run(self) -> None:
        shown = self.run_suite("""
            import os, unittest
            class Short(unittest.TestCase):
                def test_one(self): os._exit(0)
                def test_two(self): pass
        """)
        self.assertEqual(shown.returncode, 1)
        self.assertIn("exit code 0 before the run finished", shown.stderr)

    def test_a_class_setup_that_skips_its_tests_fails_the_run(self) -> None:
        shown = self.run_suite("""
            import unittest
            class Hidden(unittest.TestCase):
                @classmethod
                def setUpClass(cls): raise unittest.SkipTest("hides two tests")
                def test_one(self): pass
                def test_two(self): pass
            class Seen(unittest.TestCase):
                def test_three(self): pass
        """)
        self.assertEqual(shown.returncode, 1)
        self.assertIn("ran 1 of 3 discovered tests", shown.stderr)

    def test_a_run_that_finds_no_tests_fails(self) -> None:
        shown = self.run_suite("")
        self.assertEqual(shown.returncode, 1)
        self.assertIn("ran 0 of 0 discovered tests", shown.stderr)

    def test_a_failing_test_fails_the_run(self) -> None:
        shown = self.run_suite("""
            import unittest
            class Failing(unittest.TestCase):
                def test_one(self): self.fail("no")
        """)
        self.assertEqual(shown.returncode, 1)
        self.assertNotIn("before the run finished", shown.stderr)


if __name__ == "__main__":
    unittest.main()
