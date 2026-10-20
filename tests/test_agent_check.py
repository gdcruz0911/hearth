from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_sessions import FAKE

# detach-unmarked: detach, after also leaving work no longer below it and without the run's marker, which Hearth cannot
# establish as the run's, so reports rather than stops.
UNMARKED = """
import os, subprocess, sys
if os.environ.get("FAKE_SESSION") == "detach-unmarked":
    subprocess.run([sys.executable, "-c", "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                    "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"],
                   env={key: value for key, value in os.environ.items() if key != "HEARTH_RUN"})
    os.environ["FAKE_SESSION"] = "detach"
"""
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/agent_check.py"


class AgentCheckTests(unittest.TestCase):
    def run_check(self, *args: str, scenario: str = "detach") -> tuple[subprocess.CompletedProcess, Path]:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = folder / "bin"
        bin_dir.mkdir()
        for name in ("claude", "codex"):  # The session tests' stand-in, which starts work in its own session as Codex does.
            (bin_dir / name).write_text(f"#!{sys.executable}\nimport sys\nif sys.argv[1:] == ['--version']: print('{name} 9.9'); sys.exit()\n"
                                        f"{UNMARKED}\n{FAKE}", encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        environment = {**os.environ, "HOME": str(folder), "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin", "FAKE_SESSION": scenario}
        environment.pop("HEARTH_HOME", None)
        shown = subprocess.run([sys.executable, str(SCRIPT), *args, "--out", str(folder / "out")], env=environment,
                               capture_output=True, text=True, timeout=300)
        return shown, folder / "out"

    def test_it_refuses_to_send_real_turns_without_yes(self) -> None:
        shown, out = self.run_check()
        self.assertEqual(shown.returncode, 2)
        self.assertIn("--yes", shown.stderr)
        self.assertFalse(out.exists())

    def test_stopping_and_quitting_pass_and_leave_nothing_behind(self) -> None:
        shown, out = self.run_check("--yes")
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertTrue(report["passed"])
        self.assertEqual(report["versions"], {"claude": "claude 9.9", "codex": "codex 9.9"})
        self.assertEqual([len(round_["checks"]) for round_ in report["rounds"]], [5, 4])
        self.assertTrue(report["rounds"][0]["before"])  # Snapshots name the agents' work: process IDs and commands, no environments.
        self.assertNotIn("HEARTH_RUN", json.dumps(report))
        self.assertEqual(report["claude_transcripts"], [])  # The fake Claude keeps none.
        self.assertFalse((out / "home").exists() or (out / "project").exists())

    def test_work_left_running_fails_the_check_and_is_ended(self) -> None:
        shown, out = self.run_check("--yes", scenario="detach-unmarked")
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(shown.returncode, 1, shown.stdout + shown.stderr)
        self.assertFalse(report["passed"])
        self.assertIn("FAIL nothing of Codex's runs in its worktree", shown.stdout)
        self.assertTrue(report["left_after"])  # Found after the backend quit, and ended by the check.
        self.assertEqual(subprocess.run(["pgrep", "-f", str(out)], capture_output=True).stdout, b"")
