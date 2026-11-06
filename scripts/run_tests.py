"""Run the Python suite from this checkout, and fail unless every test it discovered ran.

`python -m unittest discover` can report success short of the full set: a test's os._exit(0) ends the run with exit code 0 and
no summary, and a SkipTest from setUpClass or setUpModule hides the tests below it. On success this names the checkout and
commit, because a complete run of another checkout looks just as green.
shortcut: a SkipTest raised while importing a test module still counts as one test (TEST-4 says to use skipUnless), fail on it if one appears.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FINISHED = 64  # Not 0, so a test that ends the process with exit code 0 cannot look like a finished run.


def inner(start: str) -> int:
    program = unittest.main(module=None, argv=["unittest", "discover", "-s", start], exit=False)
    ran, discovered = program.result.testsRun, program.test.countTestCases()
    if ran != discovered or not discovered:
        print(f"FAILED: ran {ran} of {discovered} discovered tests; a class or module setup may have skipped the rest.", file=sys.stderr)
        return 1
    if not program.result.wasSuccessful():
        return 1
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"Ran {ran} of {discovered} discovered tests in {ROOT} at {commit}.", file=sys.stderr)
    return FINISHED


def main(argv: list[str]) -> int:
    if argv[:1] == ["--inner"]:
        return inner(argv[1])
    start = argv[0] if argv else "tests"
    # As `python -m unittest` does, with this checkout's src whatever folder this was started from.
    code = subprocess.run([sys.executable, "-m", "scripts.run_tests", "--inner", start], cwd=ROOT,
                          env={**os.environ, "PYTHONPATH": str(ROOT / "src")}).returncode
    if code == FINISHED:
        return 0
    if code != 1:
        print(f"FAILED: the test process ended with exit code {code} before the run finished, so tests may not have run.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
