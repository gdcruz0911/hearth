from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_sessions import FAKE

# Run before the session tests' stand-in. check: start a helper in its own session, as Codex starts its MCP server, then three
# seconds later the requested check in another, logging both, then hang. detach-unmarked: check, after also leaving work no
# longer below the agent and without the run's marker, which Hearth cannot establish as the run's, so reports rather than stops.
SCENARIOS = """
import json, os, subprocess, sys, time
from pathlib import Path
def start(*args):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *args], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid
if os.environ.get("FAKE_SESSION") == "detach-unmarked":
    subprocess.run([sys.executable, "-c", "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                    "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"],
                   env={key: value for key, value in os.environ.items() if key != "HEARTH_RUN"})
    os.environ["FAKE_SESSION"] = "check"
if os.environ.get("FAKE_SESSION") == "check":
    helper = start("mcp-helper")
    time.sleep(3)
    test = start("-m", "unittest", "discover", "-s", "tests")  # The command line the requested check's Python process shows.
    with open(Path.home() / ".fake-check.jsonl", "a") as log:
        log.write(json.dumps({"agent": Path(sys.argv[0]).name, "helper": helper, "test": test}) + "\\n")
    os.environ["FAKE_SESSION"] = "hang"
"""
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/agent_check.py"


class AgentCheckTests(unittest.TestCase):
    def run_check(self, *args: str, scenario: str = "check") -> tuple[subprocess.CompletedProcess, Path]:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = self.folder / "bin"
        bin_dir.mkdir()
        for name in ("claude", "codex"):
            (bin_dir / name).write_text(f"#!{sys.executable}\nimport sys\nif sys.argv[1:] == ['--version']: print('{name} 9.9'); sys.exit()\n"
                                        f"{SCENARIOS}\n{FAKE}", encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        environment = {**os.environ, "HOME": str(self.folder), "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin", "FAKE_SESSION": scenario}
        environment.pop("HEARTH_HOME", None)
        shown = subprocess.run([sys.executable, str(SCRIPT), *args, "--out", str(self.folder / "out")], env=environment,
                               capture_output=True, text=True, timeout=420)  # Longer than the script's own wait, so it cleans up.
        return shown, self.folder / "out"

    def started(self) -> list[dict]:
        return [json.loads(line) for line in (self.folder / ".fake-check.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_it_refuses_to_send_real_turns_without_yes_or_with_an_unknown_ref(self) -> None:
        for args, said in (((), "--yes"), (("--yes", "--ref", "no-such-ref"), "not a commit")):
            with self.subTest(args=args):
                shown, out = self.run_check(*args)
                self.assertEqual(shown.returncode, 2)
                self.assertIn(said, shown.stderr)
                self.assertFalse(out.exists())

    def test_stopping_and_quitting_pass_and_leave_nothing_behind(self) -> None:
        shown, out = self.run_check("--yes")  # The checkout's src, so a change to stopping not yet committed is what is checked.
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertTrue(report["passed"])
        self.assertEqual(report["source"], "checkout")
        self.assertIn("uncommitted_src", report)
        self.assertEqual(report["versions"], {"claude": "claude 9.9", "codex": "codex 9.9"})
        self.assertEqual([len(round_["checks"]) for round_ in report["rounds"]], [6, 5])
        # Each round stopped only once the requested check ran: the helper started three seconds earlier was not taken for it.
        started = self.started()
        codex = [entry for entry in started if entry["agent"] == "codex"]
        self.assertEqual([round_["test_commands"]["codex"]["pid"] for round_ in report["rounds"]], [entry["test"] for entry in codex])
        self.assertEqual(report["rounds"][0]["test_commands"]["claude"]["pid"], next(entry["test"] for entry in started if entry["agent"] == "claude"))
        self.assertIn(codex[0]["helper"], [process["pid"] for process in report["rounds"][0]["captured"]])  # And it was checked as ended.
        self.assertNotIn("HEARTH_RUN", json.dumps(report))  # Snapshots name commands, never environments.
        self.assertEqual(report["claude_transcripts"], [])  # The fake Claude keeps none.
        self.assertFalse((out / "home").exists() or (out / "project").exists())

    def test_work_left_running_on_a_commit_fails_the_check_and_is_ended(self) -> None:
        shown, out = self.run_check("--yes", "--ref", "HEAD", scenario="detach-unmarked")
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(shown.returncode, 1, shown.stdout + shown.stderr)
        self.assertFalse(report["passed"])
        head = subprocess.run(["git", "-C", str(SCRIPT.parents[1]), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertEqual((report["source"], report["commit"]), ("commit", head))
        self.assertNotIn("uncommitted_src", report)  # A commit is exactly what ran, whatever the checkout holds.
        self.assertIn("FAIL nothing of Codex's runs in its worktree", shown.stdout)
        self.assertTrue(report["left_after"])  # Found after the backend quit, and ended by the check.
        self.assertEqual(subprocess.run(["pgrep", "-f", str(out)], capture_output=True).stdout, b"")
