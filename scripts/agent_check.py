"""Check stopping and quitting with the real agents before a release that affects execution.

Run from the checkout: `.venv/bin/python scripts/agent_check.py --yes [--ref <commit>]`. It sends real Claude and Codex turns,
which use the person's plans, so it never runs without --yes. Hearth's backend runs in a new scratch folder with its own home,
a clone of this repository for the agents to work in, and its own port, so the person's records and the installed app are
never touched. With --ref, the backend and the clone are that commit exactly, which release evidence needs; without it, the
backend runs the checkout's src as it is now, and the report says whether that had uncommitted changes.

Each round waits until the requested check's own Python process runs below the agent, not just a helper such as an MCP server,
and records the identity (process ID, group, and start time) of it and of every other command the agent started:

- A: stop Codex while Claude runs its check; each of Codex's commands must have ended, and Claude's check must still run.
- B: quit while Codex runs its check; every command must have ended, and every turn must be recorded as stopped by quitting.

The report (agent versions, models, the commit, timings, those identities, process snapshots with program names but no arguments or environments, and each
check) is written to <folder>/report.json, and the scratch data is removed unless --keep is given. Claude keeps its own
transcripts of these turns in ~/.claude/projects, outside the scratch folder; they are the provider's records, so the report
lists them and nothing deletes them. This does not replace the installed-app check, which also exercises packaging, the
window's lifecycle, and the shipped backend.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shlex
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
from hearth.workbench import tasks, transcript  # noqa: E402

AGENTS = ("claude", "codex")
TEST_ARGS = "-m unittest discover -s tests"  # The requested check, as its Python process's command line shows it.
WAIT_SECONDS = 300  # For an agent to start the check; real agents take seconds, a slow sign-in longer.


class Failed(Exception):
    pass


class Check:
    def __init__(self, out: Path, ref: str | None) -> None:
        self.out, self.home, self.project, self.ref = out, out / "home", out / "project", ref
        # With a ref, the backend and the agents' project are that commit exactly, as release evidence must be; without one, the
        # backend runs the checkout's src, so a change not yet committed is what is checked, and the report says so.
        self.report: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "commit": ref or _git("rev-parse", "HEAD"),
                             "source": "commit" if ref else "checkout", "rounds": []}
        if not ref:
            self.report["uncommitted_src"] = bool(_git("status", "--porcelain", "--", "src"))
        self.src = self.project / "src" if ref else REPO / "src"
        self.backend: subprocess.Popen | None = None
        self.log = None
        self.watched: list[dict] = []  # Every command captured before a stop, ended at the end if it still runs as itself.
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
        subprocess.run(["git", "-C", str(self.project), "checkout", "-q", "--detach", self.report["commit"]], check=True)
        self.home.mkdir(parents=True)
        self.check = f"PYTHONPATH=src {shlex.quote(sys.executable)} {TEST_ARGS}"
        # Codex is told not to run the project's check itself (ADR-0047), so it is asked for its turn_check: the same full
        # run, worded differently so it is not that check, and as long, so it is still running when Stop and Quit come.
        self.turn_check = f"{self.check} -p 'test_*.py'"
        (self.home / "projects.json").write_text(json.dumps(
            {"hearth": {"path": str(self.project), "check": self.check, "turn_check": self.turn_check, "providers": list(AGENTS),
                        "pr": False}}), encoding="utf-8")
        models = Path.home() / ".hearth/models.json"  # The person's default models, so the check uses what they use.
        if models.is_file():
            shutil.copy(models, self.home / "models.json")
            self.report["models"] = json.loads(models.read_text(encoding="utf-8"))

    def start(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.log = (self.out / "backend.log").open("w", encoding="utf-8")
        self.backend = subprocess.Popen(
            [sys.executable, "-m", "hearth.cli", "--database", str(self.out / "hearth.sqlite"), "web", "--port", str(port), "--no-open"],
            cwd=self.out, stdout=self.log, stderr=self.log, start_new_session=True,
            env={**os.environ, "HEARTH_HOME": str(self.home), "PYTHONPATH": str(self.src)})
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

    def escaped(self, session: str) -> list[int]:
        """The processes below the session's agent outside its group: its commands and helpers, which the bug this checks left running."""
        agent = self.run_record(session).get("pid")
        found = tasks._processes(tasks._cleanup()) or {}
        return [pid for pid in tasks._below(found, agent) if found[pid][1] != agent] if agent in found else []

    def test_command(self, session: str) -> dict | None:
        """The requested check's own Python process below the agent, with its identity; a helper such as an MCP server, or the
        shell that starts the check, is not it. Command lines are matched in memory only; the report keeps the program."""
        for pid, command in _commands(self.escaped(session)).items():
            program = command.partition(f" {TEST_ARGS}")[0]
            if program != command and Path(program).name.lower().startswith("python"):
                identity = tasks._identity(pid)
                if identity:
                    return {"pid": pid, "identity": identity, "program": Path(program).name}
        return None

    def ended(self, agent: str) -> str | None:
        """How the agent's turn ended, in its own words, or None while it runs; a usage limit or an expired sign-in then reads
        as what it is, not as a check that never started."""
        session = self.sessions[agent]
        run = self.run_record(session)
        if not run.get("finished"):
            return None
        events = self.home / "tasks" / session / "runs" / run["dir"] / "events.jsonl"
        said = [item["text"] for line in (events.read_text(encoding="utf-8", errors="replace").splitlines() if events.exists() else [])
                for item in transcript._items(run["provider"], line) if item["kind"] in ("message", "error") and item["text"]]
        return f"{agent}'s turn ended {run.get('outcome')}" + (f" ({run['error']})" if run.get("error") else "") + \
            (f", saying: {said[-1]}" if said else ", saying nothing")

    def wait_tests(self, *agents: str) -> dict:
        for _ in range(WAIT_SECONDS):
            found = {agent: self.test_command(self.sessions[agent]) for agent in agents}
            if all(found.values()):
                return found
            ended = [why for agent in agents if not found[agent] and (why := self.ended(agent))]
            if ended:  # It will not start now, so waiting longer would only hide why.
                raise Failed("The requested check did not start: " + "; ".join(ended))
            time.sleep(1)
        others = {agent: list(_programs(self.escaped(self.sessions[agent])).values()) for agent in agents}
        raise Failed(f"The requested check did not start within {WAIT_SECONDS} seconds; other commands below the agents: {others}")

    def captured(self, agent: str) -> list[dict]:
        """Every command below the agent with its identity, taken just before it is stopped, to show each ended."""
        captured = [{"pid": pid, "identity": identity, "program": program} for pid, program in _programs(self.escaped(self.sessions[agent])).items()
                    if (identity := tasks._identity(pid))]
        self.watched += captured
        return captured

    def round_a(self) -> None:
        self.sessions = {agent: self.api("POST", "sessions", {"project": "hearth", "provider": agent, "mode": "edit", "effort": "medium",
                                                              "request": f"agent-check-{agent}-{time.time_ns()}"})["session"]["id"]
                         for agent in AGENTS}
        for agent, session in self.sessions.items():
            check = self.turn_check if agent == "codex" else self.check
            self.send(session, f"Run the full check exactly once: `{check}`. Do not change any files. Reply with its final summary line only.")
        tests = self.wait_tests(*AGENTS)
        claude, codex = self.sessions["claude"], self.sessions["codex"]
        claude_agent, captured = self.run_record(claude), self.captured("codex")
        claude_agent = {"pid": claude_agent["pid"], "identity": claude_agent["pid_identity"]}
        before = _snapshot(self.out)
        started = time.monotonic()
        self.api("POST", f"sessions/{codex}/stop", {"run": self.run_record(codex)["dir"]})
        seconds = round(time.monotonic() - started, 1)
        time.sleep(3)
        record = self.run_record(codex)
        self.report["rounds"].append({"name": "A: stop Codex while Claude works", "seconds": seconds, "test_commands": tests, "captured": captured,
                                      "before": before, "after": _snapshot(self.out), "checks": {
            "Codex's check and every command captured before the stop have ended": _ended([tests["codex"], *captured]),
            "nothing of Codex's runs in its worktree": _in(self.worktree(codex)) == [],
            "Claude still runs": _running(claude_agent),
            "Claude's check, the same process, still runs": _running(tests["claude"]),
            "nothing is left paused": _paused(self.out) == [],
            "Codex's turn is recorded as stopped by the keeper, with nothing left": _stopped(record, "keeper"),
        }})

    def round_b(self) -> None:
        codex = self.sessions["codex"]
        self.send(codex, f"Run the full check exactly once more: `{self.turn_check}`. Do not change any files. Reply with its final summary line only.")
        tests = self.wait_tests("codex")
        captured = self.captured("codex") + self.captured("claude")
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
        self.report["rounds"].append({"name": "B: quit while Codex's check runs", "seconds": seconds, "test_commands": tests, "captured": captured,
                                      "before": before, "after": _snapshot(self.out), "checks": {
            "Codex's check and every command captured before quitting have ended": _ended([tests["codex"], *captured]),
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
        if self.log:
            self.log.close()
        # A failure of what this checks; ended so the check never leaves its own work behind, whether it works in the scratch
        # folder or is a captured command, such as a helper, working elsewhere and still running as itself.
        left = sorted(set(_in(self.out) or []) | {process["pid"] for process in self.watched if tasks._identity(process["pid"]) == process["identity"]})
        if left:
            self.report["left_after"] = _snapshot(self.out, left)
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


def _commands(pids: list[int], column: str = "command") -> dict[int, str]:
    """Each process's command line, or with column "comm" its program alone; never its environment."""
    shown = subprocess.run(["ps", "-o", f"pid=,{column}=", "-p", ",".join(map(str, pids)) or "0"], capture_output=True, text=True, errors="replace")
    return {int(pid): command for pid, command in (line.strip().split(" ", 1) for line in shown.stdout.splitlines() if " " in line.strip())}


def _programs(pids: list[int]) -> dict[int, str]:
    """Each process's program name, which is what the report keeps: arguments can hold anything an agent typed."""
    return {pid: Path(program).name for pid, program in _commands(pids, "comm").items()}


def _running(process: dict) -> bool:
    """Still the same process, and neither paused nor ended but not yet reaped, which keeps its identity in ps."""
    state = (tasks._processes(tasks._cleanup()) or {}).get(process["pid"], (0, 0, "Z", 0))[2]
    return tasks._identity(process["pid"]) == process["identity"] and state[:1] not in ("Z", "T")


def _ended(captured: list[dict]) -> bool:
    """None of the processes is still running as itself; a reused process ID is another process."""
    return all(tasks._identity(process["pid"]) != process["identity"] for process in captured)


def _snapshot(folder: Path, pids: list[int] | None = None) -> list[dict]:
    """Process IDs, groups, states, and programs only; never arguments or environments."""
    found = tasks._processes(tasks._cleanup()) or {}
    if pids is None:
        pids = _in(folder) or []
    programs = _programs(pids)
    return [{"pid": pid, "ppid": found[pid][0], "pgid": found[pid][1], "stat": found[pid][2], "program": programs.get(pid, "")}
            for pid in pids if pid in found]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true", help="Run real Claude and Codex turns, which use your plans.")
    parser.add_argument("--out", type=Path, help="The scratch folder, which must not exist (default ~/hearth-agent-check-<time>).")
    parser.add_argument("--ref", help="Check this commit exactly, as release evidence, instead of the checkout's src as it is now.")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch home and clone for inspection.")
    args = parser.parse_args(argv)
    if not args.yes:
        parser.error("this sends real Claude and Codex turns; add --yes to run it")
    missing = [agent for agent in AGENTS if not shutil.which(agent)]
    if missing:
        parser.error(f"{', '.join(missing)} not found on PATH")
    ref = None
    if args.ref:
        ref = _git("rev-parse", "--verify", "--quiet", f"{args.ref}^{{commit}}")
        if not ref:
            parser.error(f"{args.ref} is not a commit in this checkout")
    out = (args.out or Path.home() / f"hearth-agent-check-{time.strftime('%Y%m%d-%H%M%S')}").expanduser().resolve()  # The backend runs inside it.
    out.mkdir(parents=True)
    passed = Check(out, ref).run()
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
