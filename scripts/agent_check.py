"""Check stopping and quitting with the real agents before a release that affects execution.

Run from the checkout: `.venv/bin/python scripts/agent_check.py --yes`. It sends real Claude and Codex turns, which use the
person's plans, so it never runs without --yes. The checkout's own backend runs in a new scratch folder with its own home,
a clone of this repository for the agents to work in, and its own port, so the person's records and the installed app are
never touched. Two rounds, each with both agents running the project's full check:

- A: stop Codex while Claude keeps working; nothing of Codex's may remain, and Claude and its work must keep running.
- B: quit while Codex's own commands run; nothing may remain, and every turn must be recorded as stopped by quitting.

The report (agent versions, models, timings, process snapshots without environments, and each check) is written to
<folder>/report.json, and the scratch data is removed unless --keep is given. Claude keeps its own transcripts of these turns in
~/.claude/projects, outside the scratch folder; the report lists them. This does not replace the installed-app check, which also
exercises packaging, the window's lifecycle, and the shipped backend.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from hearth.workbench import tasks  # noqa: E402

AGENTS = ("claude", "codex")
WAIT_SECONDS = 300  # For an agent to start its command; real agents take seconds, a slow sign-in longer.


class Failed(Exception):
    pass


class Check:
    def __init__(self, out: Path) -> None:
        self.out, self.home, self.project = out, out / "home", out / "project"
        self.report: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "commit": _git("rev-parse", "HEAD"),
                             # The backend runs the checkout's src, so a change not yet committed is what was checked.
                             "uncommitted_src": bool(_git("status", "--porcelain", "--", "src")), "rounds": []}
        self.backend: subprocess.Popen | None = None
        self.opener = self.token = self.base = None

    def run(self) -> bool:
        try:
            self.prepare()
            self.start()
            self.round_a()
            self.round_b()
        except Failed as failure:
            self.report["error"] = str(failure)
        except Exception as error:  # Recorded, so the processes are still cleaned up and the report still written.
            self.report["error"] = f"{type(error).__name__}: {error}"
        finally:
            self.clean_up()
        self.report["passed"] = "error" not in self.report and all(
            all(round_["checks"].values()) for round_ in self.report["rounds"]) and not self.report.get("left_after")
        (self.out / "report.json").write_text(json.dumps(self.report, indent=1) + "\n", encoding="utf-8")
        return self.report["passed"]

    def prepare(self) -> None:
        self.report["versions"] = {agent: _version(agent) for agent in AGENTS}
        subprocess.run(["git", "clone", "-q", str(REPO), str(self.project)], check=True)
        self.home.mkdir(parents=True)
        self.check = f"PYTHONPATH=src {sys.executable} -m unittest discover -s tests"
        (self.home / "projects.json").write_text(json.dumps(
            {"hearth": {"path": str(self.project), "check": self.check, "providers": list(AGENTS), "pr": False}}), encoding="utf-8")
        models = Path.home() / ".hearth/models.json"  # The person's default models, so the check uses what they use.
        if models.is_file():
            shutil.copy(models, self.home / "models.json")
            self.report["models"] = json.loads(models.read_text(encoding="utf-8"))

    def start(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        log = (self.out / "backend.log").open("w", encoding="utf-8")
        self.backend = subprocess.Popen(
            [sys.executable, "-m", "hearth.cli", "--database", str(self.out / "hearth.sqlite"), "web", "--port", str(port), "--no-open"],
            cwd=self.out, stdout=log, stderr=log, start_new_session=True,
            env={**os.environ, "HEARTH_HOME": str(self.home), "PYTHONPATH": str(REPO / "src")})
        url = None
        for _ in range(150):
            url = next((word for word in (self.out / "backend.log").read_text(encoding="utf-8").split() if "/launch/" in word), None)
            if url:
                break
            time.sleep(0.2)
        if not url:
            raise Failed("The backend did not start; see backend.log.")
        self.base = f"http://127.0.0.1:{port}/"
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect())
        try:
            self.opener.open(url, timeout=30)
            raise Failed("The launch link did not redirect.")
        except urllib.error.HTTPError as redirect:
            self.token = redirect.headers["Location"].split("session=")[1]

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(self.base + "api/" + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"X-Hearth-Session": self.token, "Origin": self.base.rstrip("/"), "Content-Type": "application/json"})
        try:
            return json.load(self.opener.open(request, timeout=120))
        except urllib.error.HTTPError as error:
            raise Failed(f"{method} {path}: {json.load(error).get('error')}") from error

    def send(self, session: str, message: str) -> None:
        self.api("POST", f"sessions/{session}/send", {"message": message, "send": f"check-{time.time_ns()}"})

    def run_record(self, session: str) -> dict:
        return tasks._read(self.home / "tasks" / session)["runs"][-1]

    def worktree(self, session: str) -> Path:
        return Path(tasks._read(self.home / "tasks" / session)["worktree"])

    def working(self, session: str) -> list[int]:
        """The processes below the session's agent outside its group: its commands, which the bug this checks left running."""
        agent = self.run_record(session).get("pid")
        found = tasks._processes(tasks._cleanup()) or {}
        return [pid for pid in tasks._below(found, agent) if found[pid][1] != agent] if agent in found else []

    def wait_working(self, *sessions: str) -> None:
        for _ in range(WAIT_SECONDS):
            if all(self.working(session) for session in sessions):
                return
            time.sleep(1)
        raise Failed(f"No command started within {WAIT_SECONDS} seconds in {', '.join(sessions)}.")

    def round_a(self) -> None:
        message = f"Run the full check exactly once: `{self.check}`. Do not change any files. Reply with its final summary line only."
        self.sessions = {agent: self.api("POST", "sessions", {"project": "hearth", "provider": agent, "mode": "edit", "effort": "medium",
                                                              "request": f"agent-check-{agent}-{time.time_ns()}"})["session"]["id"]
                         for agent in AGENTS}
        for session in self.sessions.values():
            self.send(session, message)
        self.wait_working(*self.sessions.values())
        claude, codex = self.sessions["claude"], self.sessions["codex"]
        claude_agent, claude_work = self.run_record(claude)["pid"], self.working(claude)
        before = _snapshot(self.out)
        started = time.monotonic()
        self.api("POST", f"sessions/{codex}/stop", {"run": self.run_record(codex)["dir"]})
        seconds = round(time.monotonic() - started, 1)
        time.sleep(3)
        record = self.run_record(codex)
        self.report["rounds"].append({"name": "A: stop Codex while Claude works", "seconds": seconds, "before": before, "after": _snapshot(self.out),
                                      "checks": {
            "nothing of Codex's runs in its worktree": _in(self.worktree(codex)) == [],
            "Claude still runs": tasks._alive(claude_agent),
            "Claude's command still runs": any(tasks._alive(pid) for pid in claude_work),
            "nothing is left paused": _paused(self.out) == [],
            "Codex's turn is recorded as stopped by the keeper, with nothing left": _stopped(record, "keeper"),
        }})

    def round_b(self) -> None:
        codex = self.sessions["codex"]
        self.send(codex, f"Run the full check exactly once more: `{self.check}`. Do not change any files. Reply with its final summary line only.")
        self.wait_working(codex)
        before = _snapshot(self.out)
        started = time.monotonic()
        self.backend.send_signal(signal.SIGTERM)  # As the app does when it quits.
        try:
            self.backend.wait(timeout=120)
        except subprocess.TimeoutExpired as timeout:
            raise Failed("The backend did not stop within 120 seconds of being asked.") from timeout
        seconds = round(time.monotonic() - started, 1)
        time.sleep(3)
        records = {agent: self.run_record(session) for agent, session in self.sessions.items()}
        self.report["rounds"].append({"name": "B: quit while Codex's command runs", "seconds": seconds, "before": before, "after": _snapshot(self.out),
                                      "checks": {
            "nothing runs in the scratch folder": _in(self.out) == [],
            "nothing is left paused": _paused(self.out) == [],
            "Codex's turn is recorded as stopped by quitting, with nothing left": _stopped(records["codex"], "quit"),
            # Claude may have finished its check before the quit; then its turn is done, which is also accurate.
            "Claude's turn is recorded accurately": records["claude"].get("outcome") == "done" or _stopped(records["claude"], "quit"),
        }})

    def clean_up(self) -> None:
        if self.backend and self.backend.poll() is None:
            self.backend.send_signal(signal.SIGTERM)
            try:
                self.backend.wait(timeout=120)
            except subprocess.TimeoutExpired:
                self.backend.kill()
        transcripts = Path.home() / ".claude/projects"  # Named by Claude after the folder it ran in.
        self.report["claude_transcripts"] = sorted(str(path) for path in transcripts.glob(re.sub(r"[^A-Za-z0-9]", "-", str(self.home)) + "-*"))
        left = _in(self.out) or []
        if left:  # A failure of what this checks; ended so the check never leaves its own work behind.
            self.report["left_after"] = _snapshot(self.out)
            for pid in left:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).stdout.strip()


def _version(agent: str) -> str | None:
    try:
        shown = subprocess.run([agent, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (shown.stdout.strip().splitlines() or [None])[0]


def _in(folder: Path) -> list[int] | None:
    """Processes working in the folder, whenever they started; None when they cannot be listed, which fails a check."""
    cleanup = tasks._cleanup()
    found = tasks._in_folder(folder, 0, cleanup)
    return None if cleanup["unchecked"] else found


def _paused(folder: Path) -> list[int] | None:
    found, pids = tasks._processes(tasks._cleanup()), _in(folder)
    return None if found is None or pids is None else [pid for pid in pids if "T" in found.get(pid, (0, 0, "", 0))[2]]


def _stopped(record: dict, by: str) -> bool:
    return (record.get("outcome"), record.get("stopped_by")) == ("cancelled", by) and not any(
        record.get(key) for key in ("left_running", "left_in_folder", "cleanup_unchecked"))


def _snapshot(folder: Path) -> list[dict]:
    """Process IDs, groups, states, and commands only; never environments."""
    found, pids = tasks._processes(tasks._cleanup()) or {}, _in(folder) or []
    shown = subprocess.run(["ps", "-o", "pid=,command=", "-p", ",".join(map(str, pids)) or "0"], capture_output=True, text=True, errors="replace")
    commands = dict(line.strip().split(" ", 1) for line in shown.stdout.splitlines() if " " in line.strip())
    return [{"pid": pid, "ppid": found[pid][0], "pgid": found[pid][1], "stat": found[pid][2], "command": commands.get(str(pid), "")[:120]}
            for pid in pids if pid in found]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true", help="Run real Claude and Codex turns, which use your plans.")
    parser.add_argument("--out", type=Path, help="The scratch folder, which must not exist (default ~/hearth-agent-check-<time>).")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch home and clone for inspection.")
    args = parser.parse_args(argv)
    if not args.yes:
        parser.error("this sends real Claude and Codex turns; add --yes to run it")
    missing = [agent for agent in AGENTS if not shutil.which(agent)]
    if missing:
        parser.error(f"{', '.join(missing)} not found on PATH")
    out = (args.out or Path.home() / f"hearth-agent-check-{time.strftime('%Y%m%d-%H%M%S')}").expanduser()
    out.mkdir(parents=True)
    passed = Check(out).run()
    if not args.keep:
        shutil.rmtree(out / "home", ignore_errors=True)
        shutil.rmtree(out / "project", ignore_errors=True)
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    for round_ in report["rounds"]:
        print(f"{round_['name']} ({round_['seconds']} s)")
        for check, held in round_["checks"].items():
            print(f"  {'ok  ' if held else 'FAIL'} {check}")
    if report.get("error"):
        print(f"Stopped: {report['error']}")
    print(f"{'Passed' if passed else 'Failed'}; report in {out / 'report.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
