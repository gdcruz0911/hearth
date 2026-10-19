"""Direct sessions (ADR-0041): create, send, resume, stop, recover, and the guards that keep them out of task commands."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from hearth.workbench import sessions, stats, tasks, transcript
from test_tasks import TaskTestCase

# A stand-in for both CLIs: it logs how it was called, then prints that CLI's events. FAKE_SESSION picks the
# scenario: reply (default), hang (some partial text, then waits to be stopped), detach (hang after starting work in its
# own session, as Codex runs its commands, that starts more in another), detach-wait (detach, and on SIGTERM waits for
# that work to end), term-spawn (on SIGTERM starts work in its own session, then exits; -unmarked without the run's marker),
# group-ignore (hang with a command in its group that ignores SIGTERM), or fail.
FAKE = r'''
import json, os, re, sys, time
from pathlib import Path
codex = Path(sys.argv[0]).name == "codex"
prompt = sys.stdin.read()
scenario = os.environ.get("FAKE_SESSION", "reply")
with open(Path.home() / ".fake-session-calls.jsonl", "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt, "codex_home": os.environ.get("CODEX_HOME")}) + "\n")
def emit(event):
    print(json.dumps(event), flush=True)
args = sys.argv[1:]
model = args[args.index("-m" if codex else "--model") + 1] if ("-m" if codex else "--model") in args else None
if scenario in ("detach", "detach-wait"):
    import signal, subprocess
    work = subprocess.Popen([sys.executable, "-c", DETACHED, "1", DETACHED], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if scenario == "detach-wait":  # Asked to stop, it waits for its command to end first, as an agent may.
        signal.signal(signal.SIGTERM, lambda *_: (work.wait(), sys.exit(0)))
if scenario in ("term-spawn", "term-spawn-unmarked"):  # Asked to stop, it starts a command in its own session, then exits.
    import signal, subprocess
    def on_term(*_):
        env = {key: value for key, value in os.environ.items() if scenario == "term-spawn" or key != "HEARTH_RUN"}
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (Path.home() / ".fake-child").write_text(str(child.pid))
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
if scenario == "group-ignore":  # A command in the agent's own group that ignores SIGTERM.
    import subprocess
    subprocess.Popen([sys.executable, "-c", "import os, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                      "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)", str(Path.home() / ".fake-child")],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     env={key: value for key, value in os.environ.items() if key != "HEARTH_RUN"})  # Found by its group alone.
if scenario == "fail":
    print("Not logged in. Run login.", file=sys.stderr)
    sys.exit(1)
if codex:
    thread = args[args.index("resume") + 1] if "resume" in args else "thread-0001"
    sandbox = args[args.index("--sandbox") + 1] if "--sandbox" in args else next(
        (re.match(r'sandbox_mode="(.*)"', value).group(1) for value in args if value.startswith("sandbox_mode=")), None)
    emit({"type": "thread.started", "thread_id": thread})
    rollout = Path(os.environ["CODEX_HOME"]) / "sessions/2026/10/13" / f"rollout-2026-10-13T00-00-00-{thread}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    with rollout.open("a") as file:
        file.write(json.dumps({"type": "turn_context", "payload": {"model": model or "gpt-default", "sandbox_policy": {"type": sandbox}}}) + "\n")
    if scenario in ("hang", "detach", "detach-wait", "term-spawn", "term-spawn-unmarked", "group-ignore"):
        time.sleep(60)
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Hello from codex"}})
    emit({"type": "turn.completed", "usage": {}})
else:
    session = args[args.index("--resume") + 1] if "--resume" in args else "claude-session-1"
    print('[claude-code:unrecognized_model] {"model":"%s"}' % model, file=sys.stderr, flush=True)
    emit({"type": "system", "subtype": "init", "model": model, "session_id": session})
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}, "session_id": session})
    if scenario in ("hang", "detach", "detach-wait", "term-spawn", "term-spawn-unmarked", "group-ignore"):
        time.sleep(60)
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}, "session_id": session})
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}, "session_id": session})
    emit({"type": "result", "result": "Hello", "is_error": False, "session_id": session})
'''


class SessionTestCase(TaskTestCase):
    def setUp(self) -> None:
        super().setUp()
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        for name in ("claude", "codex"):
            (bin_dir / name).write_text(f"#!{sys.executable}\n{FAKE}", encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        # The real templates with only the program replaced, so the resume and partial-message edits act on the real flags.
        # The tables are restored by the parent's patches.
        for table, real in ((tasks.PROVIDERS, PROVIDERS), (tasks.REVIEWERS, REVIEWERS)):
            for name in ("claude", "codex"):
                table[name] = [str(bin_dir / name), *real[name][1:]]
        self.write_projects(check="true", providers=("claude", "codex", "antigravity"))

    def tearDown(self) -> None:
        sessions.stop_all()  # Before the folder goes, so every turn records its end in it.
        super().tearDown()

    def calls(self) -> list[dict]:
        path = self.home / ".fake-session-calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def new(self, provider: str = "claude", mode: str = "edit", model: str | None = "fake-model") -> str:
        return sessions.create("demo", provider, mode, model, "medium", f"request-{provider}-{mode}-{time.time_ns()}")["id"]

    def settled(self, session_id: str) -> dict:
        for _ in range(200):
            detail = sessions.detail(session_id)
            if detail["turns"] and detail["turns"][-1]["finished"]:
                return detail
            time.sleep(0.05)
        self.fail("The turn did not finish.")

    def started(self, session_id: str) -> None:
        events = lambda: sorted((self.home / ".hearth/tasks" / session_id / "runs").glob("*/events.jsonl"))[-1]  # noqa: E731
        for _ in range(200):
            if events().stat().st_size:
                return
            time.sleep(0.05)
        self.fail("The turn wrote nothing.")


# Work an agent started: it logs its process ID with the folder it runs in, and the first starts a second in its own session.
DETACHED = (
    "import json, os, subprocess, sys, time\n"
    "from pathlib import Path\n"
    "with open(Path.home() / '.fake-detached.jsonl', 'a') as log: log.write(json.dumps({'cwd': os.getcwd(), 'pid': os.getpid()}) + '\\n')\n"
    "if sys.argv[1] == '1': subprocess.Popen([sys.executable, '-c', sys.argv[2], '2', sys.argv[2]], start_new_session=True)\n"
    "time.sleep(60)\n"
)
FAKE = FAKE.replace("DETACHED", repr(DETACHED))
PROVIDERS, REVIEWERS = dict(tasks.PROVIDERS), dict(tasks.REVIEWERS)  # Taken at import, before any test patches the tables.


class SessionTests(SessionTestCase):
    def test_command_guidance_matches_the_cli_and_mode_before_the_first_turn(self) -> None:
        check = "python -m unittest discover -s tests"
        self.write_projects(check=check, providers=("claude", "codex"))
        for provider, mode in (("claude", "edit"), ("claude", "read"), ("codex", "edit"), ("codex", "read")):
            with self.subTest(provider=provider, mode=mode):
                commands = sessions.detail(self.new(provider, mode))["commands"]
                self.assertEqual(commands["check"], check if (provider, mode) == ("claude", "edit") else None)
                if provider == "codex":
                    self.assertIn("workspace-write" if mode == "edit" else "read-only", commands["description"])
                elif mode == "read":
                    self.assertIn("Shell commands are unavailable", commands["description"])
        self.assertEqual(self.calls(), [])

    def test_command_guidance_uses_the_same_fallback_check_as_launch(self) -> None:
        self.write_projects(check="")
        session_id = self.new()
        self.assertEqual(sessions.detail(session_id)["commands"]["check"], "true")
        sessions.send(session_id, "Say hello", "send-0001")
        self.settled(session_id)
        self.assertIn("Bash(true *)", self.calls()[0]["argv"])

    def test_the_list_works_before_any_project_is_added(self) -> None:
        (self.home / ".hearth/projects.json").unlink()
        self.assertEqual(sessions.listing()["options"]["projects"], {})

    def test_starting_a_conversation_runs_nothing_and_the_same_request_makes_one(self) -> None:
        first = sessions.create("demo", "claude", "edit", None, "medium", "request-0001")
        again = sessions.create("demo", "claude", "edit", None, "medium", "request-0001")

        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.calls(), [])
        self.assertIsNone(first["status"])
        record = self.only_task()
        self.assertEqual((record["kind"], record["runs"], record["session"]["model"]), ("session", [], "claude-sonnet-5-5"))
        self.assertTrue(Path(record["worktree"], "README.md").exists())

    def test_a_turn_streams_partial_text_and_records_the_reported_model_and_warning(self) -> None:
        session_id = self.new()
        sessions.send(session_id, "Say hello", "send-0001")
        turn = self.settled(session_id)["turns"][0]

        (call,) = self.calls()
        self.assertIn("--include-partial-messages", call["argv"])
        self.assertEqual(call["prompt"], "Say hello")  # On stdin, never in the argument list.
        self.assertNotIn("Say hello", call["argv"])
        self.assertEqual((turn["outcome"], turn["model"], turn["model_used"]), ("done", "fake-model", "fake-model"))
        self.assertIn("unrecognized_model", turn["warnings"][0])
        items = transcript.read(session_id, turn["dir"], 0)["items"]
        self.assertEqual([item["text"] for item in items if item["kind"] in ("delta", "message")], ["Hel", "lo", "Hello"])
        self.assertEqual(sessions.detail(session_id)["status"], "done")

    def test_the_second_turn_resumes_claude_with_the_same_restrictions(self) -> None:
        session_id = self.new(mode="read")
        sessions.send(session_id, "One", "send-0001")
        self.settled(session_id)
        sessions.send(session_id, "Two", "send-0002")
        self.settled(session_id)

        first, second = self.calls()
        self.assertEqual(second["argv"][:2], ["--resume", "claude-session-1"])
        self.assertEqual(second["argv"][2:], first["argv"])  # Same tools, settings, and model; only the resume is added.
        self.assertIn("Read,Grep,Glob", second["argv"])

    def test_codex_resumes_with_the_sandbox_through_config_and_reports_its_recorded_model(self) -> None:
        session_id = self.new("codex", "read", model=None)
        sessions.send(session_id, "One", "send-0001")
        self.settled(session_id)
        sessions.send(session_id, "Two", "send-0002")
        turns = self.settled(session_id)["turns"]

        first, second = self.calls()
        self.assertEqual(first["argv"][first["argv"].index("--sandbox") + 1], "read-only")
        self.assertEqual(second["argv"][:3], ["exec", "resume", "thread-0001"])
        self.assertNotIn("--sandbox", second["argv"])
        self.assertIn('sandbox_mode="read-only"', second["argv"])
        self.assertEqual(second["codex_home"], str(self.home / ".hearth/tasks" / session_id / "codex-home"))
        self.assertEqual([(turn["model"], turn["model_used"], turn["sandbox_used"]) for turn in turns],
                         [("gpt-6-sol", "gpt-6-sol", "read-only")] * 2)

    def test_codex_says_not_reported_when_its_session_record_is_missing(self) -> None:
        self.assertEqual(sessions._codex_context(self.home, "thread-none"), (None, None))
        self.assertEqual(sessions._codex_context(self.home, None), (None, None))

    def test_a_retried_send_starts_the_turn_once(self) -> None:
        session_id = self.new()
        sessions.send(session_id, "Say hello", "send-0001")
        sessions.send(session_id, "Say hello", "send-0001")
        self.settled(session_id)
        sessions.send(session_id, "Say hello", "send-0001")

        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(len(sessions.detail(session_id)["turns"]), 1)

    def test_a_new_message_while_a_turn_runs_is_refused(self) -> None:
        os.environ["FAKE_SESSION"] = "hang"
        session_id = self.new()
        sessions.send(session_id, "Wait", "send-0001")

        with self.assertRaises(sessions.SessionError) as refused:
            sessions.send(session_id, "Another", "send-0002")
        self.assertEqual(refused.exception.status, 409)

    def test_stop_keeps_the_partial_reply_and_leaves_no_process(self) -> None:
        os.environ["FAKE_SESSION"] = "hang"
        session_id = self.new()
        sessions.send(session_id, "Wait", "send-0001")
        self.started(session_id)
        pid = tasks._read(self.home / ".hearth/tasks" / session_id)["runs"][-1]["pid"]

        sessions.stop(session_id, "01-session-claude")
        again = sessions.stop(session_id, "01-session-claude")  # Stopping twice is harmless.

        turn = sessions.detail(session_id)["turns"][0]
        self.assertEqual((turn["outcome"], turn["stopped_by"], again["status"]), ("cancelled", "keeper", "cancelled"))
        self.assertFalse(tasks._alive(pid))
        items = transcript.read(session_id, turn["dir"], 0)["items"]
        self.assertIn({"kind": "delta", "text": "Hel"}, items)
        self.assertEqual(self.only_task()["status"], "done")  # Ready for the next message, and never blocks an update.

    def test_quitting_stops_the_active_turn(self) -> None:
        os.environ["FAKE_SESSION"] = "hang"
        session_id = self.new()
        sessions.send(session_id, "Wait", "send-0001")
        self.started(session_id)

        sessions.stop_all()

        turn = sessions.detail(session_id)["turns"][0]
        self.assertEqual((turn["outcome"], turn["stopped_by"]), ("cancelled", "quit"))
        self.assertEqual(sessions._active, {})

    def test_stop_also_ends_work_the_agent_started_in_another_session(self) -> None:
        os.environ["FAKE_SESSION"] = "detach"
        session_id = self.new("codex")
        sessions.send(session_id, "Run the check", "send-0001")
        work = self.detached(session_id)

        sessions.stop(session_id, "01-session-codex")

        self.assertEqual(sessions.detail(session_id)["turns"][0]["outcome"], "cancelled")
        self.assertEqual([pid for pid in work if tasks._alive(pid)], [])

    def test_stop_ends_the_agents_work_before_the_agent_so_an_agent_waiting_for_it_exits_at_once(self) -> None:
        os.environ["FAKE_SESSION"] = "detach-wait"
        session_id = self.new("codex")
        sessions.send(session_id, "Run the check", "send-0001")
        work = self.detached(session_id)

        started = time.monotonic()
        sessions.stop(session_id, "01-session-codex")

        self.assertLess(time.monotonic() - started, 4)  # Within the polite wait, so the agent was not forced.
        self.assertEqual([pid for pid in work if tasks._alive(pid)], [])

    def test_quitting_also_ends_work_the_agent_started_in_another_session(self) -> None:
        os.environ["FAKE_SESSION"] = "detach"
        session_id = self.new("codex")
        sessions.send(session_id, "Run the check", "send-0001")
        work = self.detached(session_id)

        sessions.stop_all()

        self.assertEqual([pid for pid in work if tasks._alive(pid)], [])

    def test_a_stop_that_cannot_end_the_agents_work_says_so(self) -> None:
        os.environ["FAKE_SESSION"] = "detach"
        session_id = self.new("codex")
        sessions.send(session_id, "Run the check", "send-0001")
        work = self.detached(session_id)
        self.addCleanup(lambda: [os.kill(pid, 9) for pid in work if tasks._alive(pid)])
        kill = os.kill

        def refusing(pid: int, sig: int) -> None:
            if sig == signal.SIGKILL and pid in work:
                raise PermissionError(pid)  # As a process of another user's would.
            kill(pid, sig)
        with mock.patch.object(tasks.os, "kill", refusing):
            sessions.stop(session_id, "01-session-codex")

        turn = sessions.detail(session_id)["turns"][0]
        self.assertEqual((turn["outcome"], sorted(turn["left_running"])), ("cancelled", sorted(work)))

    def test_work_an_agent_starts_while_stopping_is_ended_by_its_marker(self) -> None:
        os.environ["FAKE_SESSION"] = "term-spawn"
        session_id = self.new("codex")
        sessions.send(session_id, "Run the check", "send-0001")
        self.started(session_id)

        sessions.stop(session_id, "01-session-codex")

        child = int((self.home / ".fake-child").read_text())
        self.addCleanup(lambda: tasks._alive(child) and os.kill(child, 9))
        turn = sessions.detail(session_id)["turns"][0]
        self.assertFalse(tasks._alive(child))
        self.assertEqual((turn["outcome"], turn["left_running"], turn["left_in_folder"]), ("cancelled", None, None))

    def test_work_without_the_marker_left_in_the_folder_is_reported_not_stopped(self) -> None:
        os.environ["FAKE_SESSION"] = "term-spawn-unmarked"
        session_id = self.new("codex")
        sessions.send(session_id, "Run the check", "send-0001")
        self.started(session_id)

        sessions.stop(session_id, "01-session-codex")

        child = int((self.home / ".fake-child").read_text())
        self.addCleanup(lambda: tasks._alive(child) and os.kill(child, 9))
        self.assertTrue(tasks._alive(child))  # Hearth cannot tell it from the person's own work there, so it is left alone.
        self.assertEqual(sessions.detail(session_id)["turns"][0]["left_in_folder"], [child])

    def test_a_command_in_the_agents_group_that_ignores_sigterm_is_still_stopped(self) -> None:
        os.environ["FAKE_SESSION"] = "group-ignore"
        session_id = self.new("codex")
        sessions.send(session_id, "Run the check", "send-0001")
        pid_file = self.home / ".fake-child"
        for _ in range(200):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.05)
        child = int(pid_file.read_text())
        self.addCleanup(lambda: tasks._alive(child) and os.kill(child, 9))

        sessions.stop(session_id, "01-session-codex")

        for _ in range(40):  # Its parent is gone, so the system reaps it shortly after.
            if not tasks._alive(child):
                break
            time.sleep(0.05)
        self.assertFalse(tasks._alive(child))

    def test_a_process_whose_start_time_changed_is_never_signalled(self) -> None:
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (other.kill(), other.wait()))
        listed = tasks._processes(tasks._cleanup())
        processes = tasks._processes
        calls = []

        def first_listing_from_before(cleanup: dict) -> dict:  # As if the listed process had exited and its ID been reused.
            calls.append(1)
            found = processes(cleanup)
            if len(calls) == 1:
                found[other.pid] = (*found[other.pid][:3], listed[other.pid][3] - 100)
            return found
        cleanup = tasks._cleanup()
        with mock.patch.object(tasks, "_processes", first_listing_from_before):
            tasks._pause_and_end(lambda found: [other.pid] if len(calls) == 1 else [], cleanup)  # Listed only before the reuse.

        state = subprocess.run(["ps", "-o", "stat=", "-p", str(other.pid)], capture_output=True, text=True).stdout
        self.assertIsNone(other.poll())
        self.assertNotIn("T", state)  # Neither paused nor killed.

    def test_a_listing_that_fails_after_pausing_still_ends_what_was_paused(self) -> None:
        work = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(lambda: (work.kill(), work.wait()))
        processes = tasks._processes
        calls = []

        def failing_from_the_third(cleanup: dict) -> dict | None:  # Listed, paused, then ps stops answering.
            calls.append(1)
            if len(calls) >= 3:
                cleanup["unchecked"] = True
                return None
            return processes(cleanup)
        cleanup = tasks._cleanup()
        with mock.patch.object(tasks, "_processes", failing_from_the_third):
            tasks._pause_and_end(lambda found: [work.pid], cleanup)

        self.assertEqual(work.wait(timeout=5), -signal.SIGKILL)  # Ended, never left paused.
        self.assertTrue(cleanup["unchecked"])

    def test_an_environment_that_is_not_text_does_not_stop_the_search(self) -> None:
        odd = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               env={**os.environb, b"HEARTH_TEST_ODD": b"\xff\xfe"})
        self.addCleanup(lambda: (odd.kill(), odd.wait()))
        time.sleep(0.2)
        cleanup = tasks._cleanup()

        found = tasks._processes(cleanup)
        self.assertEqual(tasks._marked(found, "not-anyones", cleanup, None), [])
        self.assertFalse(cleanup["unchecked"])

    def test_recovery_does_not_force_a_process_that_stopped_being_the_turn(self) -> None:
        stubborn = self.orphan("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
        time.sleep(0.3)  # Ignoring SIGTERM before it is sent.
        run = {"pid": stubborn, "pid_identity": tasks._identity(stubborn), "started": tasks._now(), "marker": None}
        identity = tasks._identity
        calls = []

        def changed_after_the_first(pid: int) -> str | None:  # As if it exited during the polite wait and its ID was reused.
            calls.append(pid)
            return identity(pid) if len(calls) <= 2 else "another process"
        with mock.patch.object(tasks, "_identity", changed_after_the_first):
            tasks._stop_pid(run, "")

        self.assertTrue(tasks._alive(stubborn))  # The force was not sent to what might be someone else.

    def test_a_stop_that_cannot_list_processes_says_so(self) -> None:
        os.environ["FAKE_SESSION"] = "detach"
        session_id = self.new("codex")
        sessions.send(session_id, "Run the check", "send-0001")
        work = self.detached(session_id)
        self.addCleanup(lambda: [os.kill(pid, 9) for pid in work if tasks._alive(pid)])

        def failing(cleanup: dict) -> None:
            cleanup["unchecked"] = True
        with mock.patch.object(tasks, "_processes", failing):
            sessions.stop(session_id, "01-session-codex")

        turn = sessions.detail(session_id)["turns"][0]
        self.assertEqual((turn["outcome"], turn["cleanup_unchecked"]), ("cancelled", True))

    def test_a_settle_after_the_home_changed_records_in_its_own_conversation(self) -> None:
        os.environ["FAKE_SESSION"] = "hang"
        session_id = self.new()
        sessions.send(session_id, "Wait", "send-0001")
        self.started(session_id)
        pid = tasks._read(self.home / ".hearth/tasks" / session_id)["runs"][-1]["pid"]
        other_home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other_home, True)
        shutil.copytree(self.home / ".hearth/tasks" / session_id, other_home / ".hearth/tasks" / session_id)
        home = os.environ["HOME"]
        os.environ["HOME"] = str(other_home)  # As the next test does while this turn's watcher is still waking.
        try:
            os.killpg(pid, signal.SIGKILL)
            for _ in range(100):
                if not sessions._active:
                    break
                time.sleep(0.05)
            time.sleep(0.2)
        finally:
            os.environ["HOME"] = home

        self.assertIsNone(tasks._read(other_home / ".hearth/tasks" / session_id)["runs"][-1]["finished"])
        self.assertEqual(sessions.detail(session_id)["turns"][0]["outcome"], "failed")

    def test_stopping_one_conversation_leaves_the_other_and_its_work_running(self) -> None:
        os.environ["FAKE_SESSION"] = "detach"
        stopped, running = self.new("codex"), self.new("claude")
        sessions.send(stopped, "Run the check", "send-0001")
        sessions.send(running, "Run the check", "send-0002")
        stopped_work, running_work = self.detached(stopped), self.detached(running)
        agent = tasks._read(self.home / ".hearth/tasks" / running)["runs"][-1]["pid"]

        sessions.stop(stopped, "01-session-codex")

        self.assertEqual([pid for pid in stopped_work if tasks._alive(pid)], [])
        self.assertTrue(all(tasks._alive(pid) for pid in [agent, *running_work]))
        self.assertEqual(sessions.detail(running)["status"], "running")

    def test_recovery_stops_a_turn_left_by_a_crashed_backend_and_never_resends(self) -> None:
        session_id = self.new()
        task_dir = self.home / ".hearth/tasks" / session_id
        orphan = self.orphan()
        task = tasks._read(task_dir)
        task["status"] = "running"
        task["runs"].append({"dir": "01-session-claude", "role": "session", "provider": "claude", "model": "fake-model", "started": tasks._now(),
                             "finished": None, "pid": orphan, "pid_identity": tasks._identity(orphan), "send_id": "send-0001", "message": "Hi"})
        (task_dir / "runs/01-session-claude").mkdir(parents=True)
        tasks._write(task_dir, task)

        sessions.recover()

        self.assertEqual(sessions.detail(session_id)["turns"][0]["outcome"], "interrupted")
        self.assertFalse(tasks._alive(orphan))
        self.assertEqual(self.calls(), [])
        sessions.send(session_id, "Hi", "send-0001")  # A retry of the interrupted send returns it, and is not run again.
        self.assertEqual(self.calls(), [])

    def test_recovery_also_ends_work_a_crashed_backends_turn_started(self) -> None:
        session_id = self.new("codex")
        task_dir = self.home / ".hearth/tasks" / session_id
        orphan = self.orphan(DETACHED, "1", DETACHED)
        log = self.home / ".fake-detached.jsonl"
        for _ in range(200):
            work = [row["pid"] for row in map(json.loads, log.read_text(encoding="utf-8").splitlines())] if log.exists() else []
            if len(work) == 2:
                break
            time.sleep(0.05)
        work.remove(orphan)
        self.addCleanup(lambda: tasks._alive(work[0]) and os.kill(work[0], 9))
        task = tasks._read(task_dir)
        task["status"] = "running"
        task["runs"].append({"dir": "01-session-codex", "role": "session", "provider": "codex", "model": "fake-model", "started": tasks._now(),
                             "finished": None, "pid": orphan, "pid_identity": tasks._identity(orphan), "send_id": "send-0001", "message": "Hi"})
        (task_dir / "runs/01-session-codex").mkdir(parents=True)
        tasks._write(task_dir, task)

        sessions.recover()

        self.assertEqual(sessions.detail(session_id)["turns"][0]["outcome"], "interrupted")
        self.assertEqual([pid for pid in (orphan, *work) if tasks._alive(pid)], [])

    def test_recovery_records_work_it_could_not_stop(self) -> None:
        session_id = self.new("codex")
        task_dir = self.home / ".hearth/tasks" / session_id
        orphan = self.orphan(DETACHED, "1", DETACHED)
        log = self.home / ".fake-detached.jsonl"
        for _ in range(200):
            work = [row["pid"] for row in map(json.loads, log.read_text(encoding="utf-8").splitlines())] if log.exists() else []
            if len(work) == 2:
                break
            time.sleep(0.05)
        work.remove(orphan)
        self.addCleanup(lambda: tasks._alive(work[0]) and os.kill(work[0], 9))
        task = tasks._read(task_dir)
        task["status"] = "running"
        task["runs"].append({"dir": "01-session-codex", "role": "session", "provider": "codex", "model": "fake-model", "started": tasks._now(),
                             "finished": None, "pid": orphan, "pid_identity": tasks._identity(orphan), "send_id": "send-0001", "message": "Hi"})
        (task_dir / "runs/01-session-codex").mkdir(parents=True)
        tasks._write(task_dir, task)
        kill = os.kill

        def refusing(pid: int, sig: int) -> None:
            if sig == signal.SIGKILL and pid == work[0]:
                raise PermissionError(pid)
            kill(pid, sig)
        with mock.patch.object(tasks.os, "kill", refusing):
            sessions.recover()

        turn = sessions.detail(session_id)["turns"][0]
        self.assertEqual((turn["outcome"], turn["left_running"]), ("interrupted", work))

    def detached(self, session_id: str) -> list[int]:
        """The process IDs of the two processes the session's agent started outside its own session, once both run."""
        worktree = Path(tasks._read(self.home / ".hearth/tasks" / session_id)["worktree"]).resolve()
        log = self.home / ".fake-detached.jsonl"
        for _ in range(200):
            rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
            pids = [row["pid"] for row in rows if Path(row["cwd"]).resolve() == worktree]
            if len(pids) == 2:
                return pids
            time.sleep(0.05)
        self.fail("The agent's work did not start.")

    def orphan(self, *code: str) -> int:
        """A process whose parent has already exited, as a crashed backend's turn is, so the system reaps it; it runs
        Python `code` with its arguments, or sleeps."""
        command = [sys.executable, "-c", *(code or ("import time; time.sleep(60)",))]
        launcher = subprocess.run([sys.executable, "-c", f"import subprocess; print(subprocess.Popen({command!r}, start_new_session=True, "
                                   "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)"], capture_output=True, text=True, check=True)
        pid = int(launcher.stdout)
        self.addCleanup(lambda: tasks._alive(pid) and os.kill(pid, 9))
        return pid

    def test_recovery_leaves_a_turn_another_running_backend_owns(self) -> None:
        session_id, turn, owner = self.new(), self.orphan(), self.orphan()
        task_dir = self.home / ".hearth/tasks" / session_id
        task = tasks._read(task_dir)
        task["status"] = "running"
        task["runs"].append({"dir": "01-session-claude", "role": "session", "provider": "claude", "model": "fake-model", "started": tasks._now(),
                             "finished": None, "pid": turn, "pid_identity": tasks._identity(turn), "owner": owner,
                             "owner_identity": tasks._identity(owner), "send_id": "send-0001", "message": "Hi"})
        (task_dir / "runs/01-session-claude").mkdir(parents=True)
        tasks._write(task_dir, task)

        sessions.recover()
        self.assertTrue(tasks._alive(turn))
        self.assertEqual(tasks.dashboard_tasks()["sessions"], [{"id": session_id, "status": "running"}])  # An update sees it.

        os.kill(owner, 9)
        for _ in range(50):
            if not tasks._alive(owner):
                break
            time.sleep(0.1)
        sessions.recover()
        self.assertFalse(tasks._alive(turn))
        self.assertEqual(sessions.detail(session_id)["turns"][0]["outcome"], "interrupted")
        self.assertEqual(tasks.dashboard_tasks()["sessions"], [])

    def test_a_failed_turn_keeps_the_cli_error(self) -> None:
        os.environ["FAKE_SESSION"] = "fail"
        session_id = self.new()
        sessions.send(session_id, "Hi", "send-0001")
        turn = self.settled(session_id)["turns"][0]

        self.assertEqual((turn["outcome"], turn["error"]), ("failed", "Not logged in. Run login."))

    def test_requests_are_refused_with_what_to_do(self) -> None:
        for arguments in (("elsewhere", "claude", "edit"), ("demo", "antigravity", "edit"), ("demo", "claude", "write")):
            with self.subTest(arguments=arguments), self.assertRaises(sessions.SessionError):
                sessions.create(*arguments, None, "medium", "request-0002")
        session_id = self.new()
        for message, send_id in (("", "send-0001"), ("Hi", "short"), ("x" * (sessions.MESSAGE_LIMIT + 1), "send-0001")):
            with self.subTest(message=message[:10], send_id=send_id), self.assertRaises(sessions.SessionError):
                sessions.send(session_id, message, send_id)
        with self.assertRaises(sessions.SessionError) as missing:
            sessions.detail("20260101-000000")
        self.assertEqual(missing.exception.status, 404)

    def test_an_editing_turn_takes_an_implement_slot_and_a_full_slot_refuses_the_send(self) -> None:
        os.environ["FAKE_SESSION"] = "hang"
        editing, reading = self.new(), self.new("codex", "read")
        sessions.send(editing, "Wait", "send-0001")
        sessions.send(reading, "Wait", "send-0001")

        self.assertEqual((tasks._busy("implement"), tasks._busy("support")), (1, 1))
        with mock.patch.dict(tasks.SLOTS, {"implement": 1}), self.assertRaises(sessions.SessionError) as full:
            sessions.send(self.new(), "Wait", "send-0001")
        self.assertIn("slots", str(full.exception))


class ReleasedReaderTests(SessionTestCase):
    RELEASE = "9372332"  # v0.3.0, the installed runtime before slice 1 and the version a rollback returns to.

    def test_the_released_reader_refuses_a_session_without_changing_it_or_launching_anything(self) -> None:
        session_id = self.new()
        record = self.home / ".hearth/tasks" / session_id / "task.json"
        before = record.read_bytes()
        self.assertEqual(json.loads(before)["format"], 2)
        released = self.home / "released"
        released.mkdir()
        archive = subprocess.run(["git", "-C", str(Path(__file__).parents[1]), "archive", self.RELEASE, "src"], capture_output=True)
        self.assertEqual(archive.returncode, 0, f"commit {self.RELEASE} is needed; fetch the full history: {archive.stderr.decode()}")
        subprocess.run(["tar", "-x", "-C", str(released)], input=archive.stdout, check=True)
        environment = {**os.environ, "PYTHONPATH": str(released / "src"), "HOME": str(self.home), "PATH": f"{self.home / 'bin'}:/usr/bin:/bin"}

        for command in (["task", "list"], ["task", "show", session_id], ["task", "cancel", session_id], ["loop", session_id]):
            with self.subTest(command=command):
                ran = subprocess.run([sys.executable, "-m", "hearth.cli", *command], capture_output=True, text=True, env=environment, timeout=120)
                self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)
                self.assertIn("written by a newer Hearth", ran.stderr)
        self.assertEqual(record.read_bytes(), before)
        self.assertFalse((record.parent / "cancel").exists())
        self.assertEqual(self.calls(), [])  # The fake CLIs on PATH were never started.


class SessionWorkTests(SessionTestCase):
    """Slice 2: drafts that survive quitting (ADR-0044), the worktree's changes, and opening its folder."""

    def test_a_late_save_never_brings_back_sent_text_or_overwrites_newer_text(self) -> None:
        session_id = self.new()
        sessions.save_draft(session_id, "Say hello", 10)
        self.assertTrue(sessions.send(session_id, "Say hello", "send-0001", draft_at=10)["draft_cleared"])
        self.settled(session_id)
        self.assertEqual(sessions.save_draft(session_id, "Say hello", 10), {"text": "", "at": 10})  # The save that raced its send.
        sessions.save_draft(session_id, "Next question", 12)
        self.assertEqual(sessions.save_draft(session_id, "Next q", 11), {"text": "Next question", "at": 12})  # An older save, late.
        self.assertEqual(sessions.detail(session_id)["draft"], {"text": "Next question", "at": 12})

    def test_a_send_does_not_clear_text_typed_after_it(self) -> None:
        session_id = self.new()
        sessions.save_draft(session_id, "Follow-up typed while sending", 20)
        sessions.send(session_id, "Say hello", "send-0001", draft_at=15)
        self.settled(session_id)
        self.assertEqual(sessions.detail(session_id)["draft"]["text"], "Follow-up typed while sending")

    def test_a_send_that_cannot_start_keeps_the_draft(self) -> None:
        session_id = self.new()
        sessions.save_draft(session_id, "Say hello", 10)
        with mock.patch.object(sessions, "_launch", side_effect=OSError("no such program")):
            sessions.send(session_id, "Say hello", "send-0001", draft_at=10)
        self.assertEqual(sessions.detail(session_id)["draft"], {"text": "Say hello", "at": 10})
        refused = self.new()
        sessions.save_draft(refused, "Too long", 10)
        with self.assertRaises(sessions.SessionError):
            sessions.send(refused, "x" * (sessions.MESSAGE_LIMIT + 1), "send-0002", draft_at=10)
        self.assertEqual(sessions.detail(refused)["draft"]["text"], "Too long")

    def test_a_draft_that_cannot_be_cleared_still_leaves_the_turn_tracked_and_stopped_on_quit(self) -> None:
        os.environ["FAKE_SESSION"] = "hang"
        session_id = self.new()
        sessions.save_draft(session_id, "Wait", 10)
        with mock.patch.object(sessions, "_write_draft", side_effect=OSError("disk full")):
            sent = sessions.send(session_id, "Wait", "send-0001", draft_at=10)  # The turn started; the draft write is not its business.
        self.assertFalse(sent["draft_cleared"])  # Said, so the page saves the cleared draft itself.
        self.started(session_id)
        pid = tasks._read(self.home / ".hearth/tasks" / session_id)["runs"][-1]["pid"]

        sessions.stop_all()

        turn = sessions.detail(session_id)["turns"][-1]
        self.assertEqual((turn["outcome"], turn["stopped_by"]), ("cancelled", "quit"))
        self.assertFalse(tasks._alive(pid))
        self.assertEqual(sessions.detail(session_id)["draft"]["text"], "Wait")  # Kept, since it could not be cleared.

    def test_a_launched_turn_whose_record_cannot_be_updated_is_still_watched_and_settled(self) -> None:
        session_id = self.new()
        real, writes = tasks._write, []
        def write(task_dir, task):
            writes.append(task_dir)
            if len(writes) == 2:  # The update just after launch; the one before it is how the turn was recorded.
                raise OSError("disk full")
            return real(task_dir, task)
        with mock.patch.object(tasks, "_write", side_effect=write), self.assertRaises(OSError):
            sessions.send(session_id, "Say hello", "send-0001")
        with self.assertRaises(sessions.SessionError) as refused:  # Unsettled, so no second agent starts beside it.
            sessions.send(session_id, "Again", "send-0002")
        self.assertEqual(refused.exception.status, 409)

        turn = self.settled(session_id)["turns"][-1]  # The watcher settles it when the agent ends.
        self.assertEqual(turn["outcome"], "done")
        self.assertEqual(len(self.calls()), 1)

    def test_a_draft_must_be_text_with_a_version(self) -> None:
        session_id = self.new()
        for text, at in (("hi", None), ("hi", True), ("hi", -1), (None, 1), ("x" * (sessions.MESSAGE_LIMIT + 1), 1)):
            with self.subTest(text=str(text)[:5], at=at), self.assertRaises(sessions.SessionError):
                sessions.save_draft(session_id, text, at)

    def test_changes_list_committed_edited_and_new_files_and_show_a_link_by_its_target(self) -> None:
        session_id = self.new()
        worktree = Path(self.only_task()["worktree"])
        (worktree / "README.md").write_text("changed\n", encoding="utf-8")
        (worktree / "committed.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "committed.txt"], check=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"], check=True)
        (worktree / "new.txt").write_text("fresh\n", encoding="utf-8")
        outside = self.home / "outside-secret.txt"
        outside.write_text("do not show\n", encoding="utf-8")
        (worktree / "link").symlink_to(outside)
        before = sorted(p.name for p in Path(subprocess.run(["git", "-C", str(worktree), "rev-parse", "--git-dir"], capture_output=True, text=True,
                                                               check=True).stdout.strip()).iterdir())

        found = sessions.changes(session_id)

        self.assertEqual(sorted((f["path"], f["status"]) for f in found["files"]),
                         [("README.md", "M"), ("committed.txt", "A"), ("link", "?"), ("new.txt", "?")])
        self.assertIn("+fresh", found["diff"])
        self.assertIn(f"+{outside}", found["diff"])
        self.assertNotIn("do not show", found["diff"])
        self.assertEqual((found["truncated"], found["untracked_hidden"]), (False, 0))
        self.assertGreater(found["added"], 0)
        after = sorted(p.name for p in Path(subprocess.run(["git", "-C", str(worktree), "rev-parse", "--git-dir"], capture_output=True, text=True,
                                                              check=True).stdout.strip()).iterdir())
        self.assertEqual(before, after)  # No index.lock left, and nothing written beside it.

    def test_changes_leave_out_the_guidance_hearth_copied_in(self) -> None:
        (self.repo / "CONTEXT.md").write_text("Project words, neither committed nor ignored.\n", encoding="utf-8")
        session_id = self.new()
        worktree = Path(self.only_task()["worktree"])
        self.assertIn("CONTEXT.md", self.only_task()["copied"])
        (worktree / "made.txt").write_text("by the agent\n", encoding="utf-8")

        self.assertEqual([f["path"] for f in sessions.changes(session_id)["files"]], ["made.txt"])

    def test_changes_take_no_index_lock(self) -> None:
        session_id = self.new()
        with mock.patch("subprocess.run", wraps=subprocess.run) as run:
            sessions.changes(session_id)
        git_calls = [call.args[0] for call in run.call_args_list if call.args and call.args[0][:1] == ["git"]]
        self.assertTrue(git_calls)
        self.assertTrue(all(argv[1] == "--no-optional-locks" for argv in git_calls))

    def test_opening_the_work_folder_opens_only_the_folder_hearth_made(self) -> None:
        session_id = self.new()
        record = self.only_task()
        with mock.patch.object(sessions.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertEqual(sessions.open_folder(session_id), {"opened": record["worktree"]})
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["open", record["worktree"]])

        failed = subprocess.CompletedProcess([], 1, "", "LSOpenURLsWithRole() failed with error -10814.\n")
        with mock.patch.object(sessions.subprocess, "run", return_value=failed), self.assertRaises(sessions.SessionError) as refused:
            sessions.open_folder(session_id)
        self.assertIn("could not open the work folder: LSOpenURLsWithRole() failed", str(refused.exception))
        with mock.patch.object(sessions.subprocess, "run", side_effect=FileNotFoundError("open")), self.assertRaises(sessions.SessionError):
            sessions.open_folder(session_id)

        task_dir = self.home / ".hearth/tasks" / session_id
        changed = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        changed["worktree"] = "/Applications/Calculator.app"
        (task_dir / "task.json").write_text(json.dumps(changed), encoding="utf-8")
        with mock.patch.object(sessions.subprocess, "run") as run, self.assertRaises(sessions.SessionError):
            sessions.open_folder(session_id)
        run.assert_not_called()


class SessionGuardTests(SessionTestCase):
    def test_task_commands_dashboard_tasks_and_stats_leave_sessions_alone(self) -> None:
        session_id = self.new()

        self.assertIsNone(tasks._resolve(session_id))
        self.assertIsNone(tasks._resolve("last"))
        self.assertEqual(self.cli("task", "list")[1], "")
        self.assertEqual(tasks.dashboard_tasks()["tasks"], [])
        self.assertIsNone(tasks.dashboard_task_state(session_id, "cancel"))
        self.assertEqual(json.loads(self.cli("stats", "--json")[1])["summary"]["tasks"], 0)
        with self.assertRaises(sessions.SessionError):
            sessions.send(self.cli_task(), "Hi", "send-0001")

    def cli_task(self) -> str:
        os.environ["FAKE_AGENT_SCENARIO"] = "edit"
        with mock.patch.dict(tasks.PROVIDERS, {"claude": __import__("test_tasks").FAKE_AGENT}):
            self.cli("task", "new", "demo", "Add hello.txt", "--model", "fake-model")
        return next(task["id"] for task in tasks.dashboard_tasks()["tasks"])


class TranscriptDeltaTests(SessionTestCase):
    def test_only_text_deltas_become_items(self) -> None:
        self.assertEqual(transcript._claude({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "Hi"}}}),
                         [{"kind": "delta", "text": "Hi"}])
        self.assertEqual(transcript._claude({"type": "stream_event", "event": {"delta": {"type": "input_json_delta", "partial_json": "{"}}}), [])
