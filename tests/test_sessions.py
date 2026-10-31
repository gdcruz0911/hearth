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
# group-ignore (hang with a command in its group that ignores SIGTERM), chain (hang after starting a chained command through a
# shell in its own group without the run's marker), or fail.
FAKE = r'''
import json, os, re, sys, time
from pathlib import Path
codex = Path(sys.argv[0]).name == "codex"
prompt = sys.stdin.read()
scenario = os.environ.get("FAKE_SESSION", "reply")
with open(Path.home() / ".fake-session-calls.jsonl", "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt, "codex_home": os.environ.get("CODEX_HOME"),
                          "no_memory": os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY")}) + "\n")
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
if scenario == "chain":
    import subprocess
    child = subprocess.Popen(["env", "-u", "HEARTH_RUN", "/bin/sh", "-c", "until [ -e go ]; do sleep 0.1; done; touch ran-after"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (Path.home() / ".fake-child").write_text(str(child.pid))
if scenario == "fail":
    print("Not logged in. Run login.", file=sys.stderr)
    sys.exit(1)
if scenario in ("edit", "edit-hang", "edit-go"):  # It changes a file in its worktree, as an editing turn does.
    Path("edited.txt").write_text(prompt)
while scenario == "edit-go" and not (Path.home() / ".fake-go").exists():  # Then finishes once the test says go.
    time.sleep(0.05)
if codex:
    thread = args[args.index("resume") + 1] if "resume" in args else "thread-0001"
    sandbox = args[args.index("--sandbox") + 1] if "--sandbox" in args else next(
        (re.match(r'sandbox_mode="(.*)"', value).group(1) for value in args if value.startswith("sandbox_mode=")), None)
    emit({"type": "thread.started", "thread_id": thread})
    rollout = Path(os.environ["CODEX_HOME"]) / "sessions/2026/10/13" / f"rollout-2026-10-13T00-00-00-{thread}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    with rollout.open("a") as file:
        file.write(json.dumps({"type": "turn_context", "payload": {"model": model or "gpt-default", "sandbox_policy": {"type": sandbox}}}) + "\n")
    if scenario in ("hang", "detach", "detach-wait", "term-spawn", "term-spawn-unmarked", "group-ignore", "chain", "edit-hang"):
        time.sleep(60)
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Hello from codex"}})
    emit({"type": "turn.completed", "usage": {}})
else:
    session = args[args.index("--resume") + 1] if "--resume" in args else "claude-session-1"
    print('[claude-code:unrecognized_model] {"model":"%s"}' % model, file=sys.stderr, flush=True)
    emit({"type": "system", "subtype": "init", "model": model, "session_id": session})
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}, "session_id": session})
    if scenario in ("hang", "detach", "detach-wait", "term-spawn", "term-spawn-unmarked", "group-ignore", "chain", "edit-hang"):
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
        conversation = mock.patch.dict(sessions.CONVERSATION, {"claude": [str(bin_dir / "claude"), *CONVERSATION["claude"][1:]]})
        conversation.start()
        self.addCleanup(conversation.stop)
        self.write_projects(check="true", providers=("claude", "codex", "antigravity"))
        quitting = mock.patch.object(sessions, "_quitting", False)  # stop_all sets it for good, as the backend then exits.
        quitting.start()
        self.addCleanup(quitting.stop)

    def tearDown(self) -> None:
        self.report_failure()  # Before stopping, so it shows what the failure left running.
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

    def orphan(self, *code: str) -> int:
        """A process whose parent has already exited, as a crashed backend's turn is, so the system reaps it; it runs
        Python `code` with its arguments, or sleeps."""
        command = [sys.executable, "-c", *(code or ("import time; time.sleep(60)",))]
        launcher = subprocess.run([sys.executable, "-c", f"import subprocess; print(subprocess.Popen({command!r}, start_new_session=True, "
                                   "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)"], capture_output=True, text=True, check=True)
        pid = int(launcher.stdout)
        self.addCleanup(lambda: tasks._alive(pid) and os.kill(pid, 9))
        return pid

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
CONVERSATION = dict(sessions.CONVERSATION)


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

    def test_a_crashed_turns_chained_command_is_stopped_though_its_shell_hides_its_marker(self) -> None:
        os.environ["FAKE_SESSION"] = "chain"
        session_id = self.new()
        task_dir = self.home / ".hearth/tasks" / session_id
        sessions.send(session_id, "Run the chain", "send-0001")
        self.started(session_id)
        child = self.home / ".fake-child"
        run = tasks._read(task_dir)["runs"][-1]
        for _ in range(200):
            recorded = Path(run["descendants"]).read_text(encoding="utf-8") if Path(run["descendants"]).exists() else ""
            if child.exists() and child.read_text(encoding="utf-8") and f"{child.read_text(encoding='utf-8')} " in recorded:
                break
            time.sleep(0.05)
        wrapper = int(child.read_text(encoding="utf-8"))
        self.addCleanup(lambda: tasks._alive(wrapper) and os.kill(wrapper, 9))
        os.kill(run["pid"], signal.SIGKILL)
        self.settled(session_id)

        tasks._stop_pid(run, tasks._read(task_dir)["worktree"])  # What recovery does for a turn whose backend crashed with it.

        (Path(tasks._read(task_dir)["worktree"]) / "go").touch()  # Lets the chain's next command run, if its shell survived.
        time.sleep(1)
        self.assertFalse(tasks._alive(wrapper))
        self.assertFalse((Path(tasks._read(task_dir)["worktree"]) / "ran-after").exists())

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
        self.assertTrue(self.running(child))  # Hearth cannot tell it from the person's own work there, so it is left alone.
        turn = sessions.detail(session_id)["turns"][0]
        self.assertEqual(turn["left_in_folder"], [child], {key: turn[key] for key in ("left_running", "cleanup_unchecked", "started")})

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

        self.assertTrue(self.running(stubborn))  # The force was not sent to what might be someone else.

    def test_a_stop_after_the_watcher_reaped_the_turn_spares_whatever_took_its_id(self) -> None:
        # The turn's own process object, already reaped by its watcher, still names the turn's ID, which a replacement now
        # holds: a leader of its own group, started later than the turn recorded, with a command in another group below it.
        turn = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"], start_new_session=True)
        recorded = tasks._identity(turn.pid).split(None, 1)[1]  # Its start time.
        turn.wait()
        time.sleep(1.1)  # A reused ID belongs to a process started later; ps shows start times to the second.
        replacement = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; child = subprocess.Popen([sys.executable, '-c', "
                                        "'import time; time.sleep(60)'], start_new_session=True); print(child.pid, flush=True); time.sleep(60)"],
                                       stdout=subprocess.PIPE, text=True, start_new_session=True)
        child = int(replacement.stdout.readline())
        self.addCleanup(lambda: (tasks._alive(child) and os.kill(child, 9), replacement.kill(), replacement.wait(), replacement.stdout.close()))
        turn.pid = replacement.pid  # The reuse itself cannot be arranged, so the reaped turn is pointed at the replacement.
        run = {"pid": replacement.pid, "pid_identity": f"{replacement.pid} {recorded}", "started": tasks._now(), "marker": None}

        cleanup = tasks._stop(turn, run, "")

        self.assertTrue(self.running(child))  # Not the turn's command: its ancestry runs through a process that is not the turn.
        self.assertIsNone(replacement.poll())  # Nor was the replacement's group signalled.
        self.assertEqual(cleanup["refused"], [])  # The turn itself has ended; the replacement is not reported as its own.

    def test_a_live_turn_whose_identity_was_never_recorded_is_still_stopped(self) -> None:
        # An unreaped child's ID cannot be another's, so a stop takes the identity it lacks; one that cannot be taken, as when
        # ps fails, leaves the turn unsignalled and reported, without waiting on it.
        for shown in (True, False):
            with self.subTest(identity_shown=shown):
                agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
                self.addCleanup(lambda agent=agent: (agent.kill(), agent.wait()))
                run = {"pid": agent.pid, "pid_identity": None, "started": tasks._now(), "marker": None}
                started = time.monotonic()
                with mock.patch.object(tasks, "_identity", tasks._identity if shown else lambda pid: None):
                    cleanup = tasks._stop(agent, run, "")
                self.assertLess(time.monotonic() - started, 4)
                if shown:
                    self.assertEqual((agent.returncode, cleanup["refused"]), (-signal.SIGTERM, []))
                else:
                    self.assertEqual((agent.returncode, cleanup["refused"]), (None, [agent.pid]))  # Shown as "could not stop".

    def test_a_turn_that_cannot_be_shown_to_be_the_run_is_reported_and_stop_returns(self) -> None:
        os.environ["FAKE_SESSION"] = "hang"
        session_id = self.new()
        sessions.send(session_id, "Hi", "send-0001")
        self.started(session_id)
        agent = tasks._read(self.home / ".hearth/tasks" / session_id)["runs"][-1]["pid"]
        started = time.monotonic()

        # Neither its identity nor its marker shows it is the turn, as for a program whose environment macOS hides.
        with mock.patch.object(tasks, "_identity", return_value="another process"), mock.patch.object(tasks, "_marked", return_value=[]):
            sessions.stop(session_id, "01-session-claude")

        self.assertLess(time.monotonic() - started, 10)  # Not waiting on a turn it could not stop.
        self.assertTrue(self.running(agent))
        self.assertEqual(sessions.detail(session_id)["turns"][0]["left_running"], [agent])

    def test_the_folder_report_compares_start_times_on_the_clock_ps_uses(self) -> None:
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        work = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=folder)
        self.addCleanup(lambda: (work.kill(), work.wait()))
        time.sleep(0.2)
        processes = tasks._processes
        started = processes(tasks._cleanup())[work.pid][3]

        def a_second_early(cleanup: dict) -> dict | None:  # As Linux's ps can report, from a boot time rounded down.
            found = processes(cleanup)
            return found and {pid: (*row[:3], row[3] - 1) for pid, row in found.items()}
        run = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),  # The same second by the wall clock.
               "pid_identity": "1 " + time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(started - 1))}  # As ps showed the agent.
        with mock.patch.object(tasks, "_processes", a_second_early):
            found = tasks._in_folder(folder, tasks._started(run), tasks._cleanup())

        self.assertEqual(found, [work.pid])

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
        self.assertTrue(all(self.running(pid) for pid in [agent, *running_work]))
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

    def running(self, pid: int) -> bool:
        """Still running as itself: listed, and neither ended but not yet reaped nor paused, which `_alive` cannot tell apart."""
        return (tasks._processes(tasks._cleanup()) or {}).get(pid, (0, 0, "Z", 0))[2][:1] not in ("Z", "T")

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


class FullSetupTests(SessionTestCase):
    """ADR-0049: a Claude conversation that edits runs with the keeper's own Claude setup, and @codex asks Codex to check it."""

    def test_an_editing_claude_conversation_runs_with_the_keepers_own_setup_and_resumes_it(self) -> None:
        session_id = self.new()
        sessions.send(session_id, "One", "send-0001")
        self.settled(session_id)
        sessions.send(session_id, "Two", "send-0002")
        self.settled(session_id)

        first, second = self.calls()
        for flag in ("--setting-sources", "--strict-mcp-config", "--tools", "--settings"):
            self.assertNotIn(flag, first["argv"])
        self.assertEqual(first["argv"][first["argv"].index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("Bash(true *)", first["argv"])
        self.assertIn("PushNotification", first["argv"][first["argv"].index("--disallowedTools") + 1].split(","))
        self.assertIsNone(first["no_memory"])
        self.assertEqual(second["argv"][:2], ["--resume", "claude-session-1"])

    def test_read_only_claude_and_codex_conversations_keep_their_restrictions(self) -> None:
        for provider in ("claude", "codex"):
            session_id = self.new(provider, "read")
            sessions.send(session_id, "One", "send-0001")
            self.settled(session_id)
        claude, codex = self.calls()
        self.assertIn("--strict-mcp-config", claude["argv"])
        self.assertEqual(claude["no_memory"], "1")
        self.assertIn("--ignore-user-config", codex["argv"])

    def test_codex_checks_the_changes_read_only_and_nothing_reaches_claude_unless_sent(self) -> None:
        os.environ["FAKE_SESSION"] = "edit"
        session_id = self.new()
        sessions.send(session_id, "Write a file", "send-0001")
        self.settled(session_id)
        os.environ["FAKE_SESSION"] = "reply"
        sessions.send(session_id, "@codex is this right?", "send-0002")
        detail = self.settled(session_id)
        sessions.send(session_id, "Carry on", "send-0003")
        detail = self.settled(session_id)

        _, ask, after = self.calls()
        self.assertEqual(ask["argv"][ask["argv"].index("--sandbox") + 1], "read-only")
        self.assertEqual(Path(ask["cwd"]).resolve(), Path(detail["worktree"]).resolve())
        for part in ("The keeper's question: is this right?", "Claude's last reply:\nHello", "New files: edited.txt"):
            self.assertIn(part, ask["prompt"])
        turn = detail["turns"][1]
        self.assertEqual((turn["role"], turn["provider"], turn["reply"]), ("ask", "codex", "Hello from codex"))
        self.assertEqual(after["argv"][:2], ["--resume", "claude-session-1"])  # Claude's own session, untouched by the ask.
        self.assertEqual(after["prompt"], "Carry on")
        self.assertNotIn("check", [run["role"] for run in self.only_task()["runs"]])

    def test_an_ask_before_claude_has_replied_does_not_become_the_conversations_session(self) -> None:
        session_id = self.new()
        sessions.send(session_id, "@codex", "send-0001")
        self.settled(session_id)
        sessions.send(session_id, "Hello", "send-0002")
        self.settled(session_id)

        ask, first = self.calls()
        self.assertIn("Check these changes for bugs", ask["prompt"])
        self.assertNotIn("--resume", first["argv"])
        self.assertEqual(self.only_task()["session"]["cli_session"], "claude-session-1")

    def test_an_ask_in_a_project_that_does_not_allow_codex_is_refused_and_sends_nothing(self) -> None:
        self.write_projects(check="true", providers=("claude",))
        session_id = self.new()
        with self.assertRaises(sessions.SessionError) as refused:
            sessions.send(session_id, "@codex check", "send-0001")
        self.assertIn("add codex to demo's providers", str(refused.exception))
        self.assertEqual((self.calls(), self.only_task()["runs"]), ([], []))

    def test_a_subagents_partial_text_never_joins_the_reply_draft(self) -> None:
        delta = {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "inner"}}}
        self.assertEqual(transcript._items("claude", json.dumps({**delta, "parent_tool_use_id": "toolu_01"})), [])
        self.assertEqual([item["text"] for item in transcript._items("claude", json.dumps(delta))], ["inner"])

    def test_a_retried_ask_starts_codex_once(self) -> None:
        session_id = self.new()
        sessions.send(session_id, "@codex check", "send-0001")
        sessions.send(session_id, "@codex check", "send-0001")
        self.settled(session_id)
        self.assertEqual(len(self.calls()), 1)

    def test_codex_and_read_only_conversations_send_at_codex_as_an_ordinary_message(self) -> None:
        for provider, mode in (("codex", "edit"), ("claude", "read")):
            session_id = self.new(provider, mode)
            sessions.send(session_id, "@codex hello", "send-0001")
            self.assertEqual(self.settled(session_id)["turns"][0]["role"], "session")
        self.assertEqual([call["prompt"] for call in self.calls()], ["@codex hello", "@codex hello"])

    def test_claude_configuration_the_agent_changed_stops_the_conversation(self) -> None:
        for path in (".claude/agents/helper.md", ".mcp.json"):
            with self.subTest(path=path):
                session_id = self.new()
                changed = Path(sessions.detail(session_id)["worktree"], path)
                changed.parent.mkdir(parents=True, exist_ok=True)
                changed.write_text("{}", encoding="utf-8")
                with self.assertRaises(sessions.SessionError) as refused:
                    sessions.send(session_id, "Hello", "send-0001")
                self.assertEqual(refused.exception.status, 409)
                self.assertIn(path, str(refused.exception))
        self.assertEqual(self.calls(), [])

    def test_tools_nobody_could_approve_are_recorded_from_the_result(self) -> None:
        text = (FIXTURES / "claude-print-2.1.283-denied.jsonl").read_text(encoding="utf-8")
        self.assertEqual(sessions._denied(text), ["mcp__claude_ai_Notion__notion-search (hearth)"])
        refused = json.dumps({"type": "result", "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "python3 -c 1"}},
                                                                       {"tool_name": "Bash", "tool_input": {"command": "python3 -c 1"}},
                                                                       {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}]})
        self.assertEqual(sessions._denied(refused), ["Bash (python3 -c 1)", "Read (/etc/hosts)"])
        self.assertIsNone(sessions._denied((FIXTURES / "claude-print-2.1.283-subagent.jsonl").read_text(encoding="utf-8")))

    def test_a_subagents_and_a_skills_text_is_shown_as_tool_output_not_as_the_reply(self) -> None:
        items = [item for line in (FIXTURES / "claude-print-2.1.283-subagent.jsonl").read_text(encoding="utf-8").splitlines()
                 for item in transcript._items("claude", line)]
        self.assertEqual([item["text"].split()[0] for item in items if item["kind"] == "message"], ["DONE"])
        self.assertEqual([item["tool"] for item in items if item.get("subagent")], ["Write"])


FIXTURES = Path(__file__).with_name("fixtures") / "public/providers"


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


class HostCheckTests(SessionTestCase):
    """ADR-0047: Codex is told its turn_check at the first turn, and Hearth runs the project's check after a Codex turn that
    changed files, outside the sandbox, launched and ended like a turn; its result opens the next message."""

    def project(self, check: str, turn_check: str | None = "echo quick") -> None:
        config = {"path": str(self.repo), "check": check, "providers": ["claude", "codex"], **({"turn_check": turn_check} if turn_check else {})}
        (self.home / ".hearth/projects.json").write_text(json.dumps({"demo": config}), encoding="utf-8")

    def check_record(self, session_id: str, finished: bool = True) -> dict:
        for _ in range(200):
            runs = tasks._read(self.home / ".hearth/tasks" / session_id)["runs"]
            if runs[-1]["role"] == sessions.CHECK and (runs[-1].get("finished") if finished else runs[-1].get("pid")):
                return runs[-1]
            time.sleep(0.05)
        self.fail("The check did not " + ("finish." if finished else "start."))

    def test_a_changing_turn_runs_the_check_and_its_result_opens_the_next_message_once(self) -> None:
        self.project("echo check-ran; exit 3")
        os.environ["FAKE_SESSION"] = "edit"
        session_id = self.new("codex")
        sessions.send(session_id, "Change it", "send-0001")
        record = self.check_record(session_id)

        self.assertEqual((record["outcome"], record["exit_code"], record["command"]), ("failed", 3, "echo check-ran; exit 3"))
        detail = sessions.detail(session_id)
        self.assertEqual(len(detail["turns"]), 1)  # The check is shown with its turn, not as a turn.
        self.assertEqual((detail["turns"][0]["check"]["tail"], detail["status"], detail["turn_count"]), ("check-ran", "done", 1))
        self.assertEqual((detail["commands"]["turn_check"], detail["commands"]["host_check"]), ("echo quick", "echo check-ran; exit 3"))
        first = self.calls()[0]
        told = (self.home / ".hearth/tasks" / session_id / "runs/01-session-codex/developer_instructions.md").read_text(encoding="utf-8")
        self.assertIn(f"developer_instructions={json.dumps(told)}", first["argv"])
        self.assertIn("`echo quick`", told)
        self.assertEqual(first["prompt"], "Change it")

        os.environ["FAKE_SESSION"] = "reply"
        sessions.send(session_id, "Next", "send-0002")
        self.settled(session_id)
        sessions.send(session_id, "Again", "send-0003")
        self.settled(session_id)

        second, third = self.calls()[1:]
        task_dir = self.home / ".hearth/tasks" / session_id
        self.assertTrue(second["prompt"].startswith("[Hearth] After your last turn"), second["prompt"])
        self.assertIn("exited with code 3", second["prompt"])
        self.assertIn("check-ran", second["prompt"])
        self.assertTrue(second["prompt"].endswith("\n\nNext"))
        self.assertEqual((task_dir / "runs/03-session-codex/prompt.md").read_text(encoding="utf-8"), second["prompt"])  # ADR-0024.
        self.assertEqual(tasks._read(task_dir)["runs"][2]["message"], "Next")
        self.assertFalse(any(part.startswith("developer_instructions=") for part in second["argv"]))  # Resumed turns ignore them.
        self.assertEqual(third["prompt"], "Again")  # Delivered once.
        self.assertEqual([turn["check"] for turn in sessions.detail(session_id)["turns"][1:]], [None, None])  # Nothing changed.

    def test_no_check_after_a_stopped_turn_or_a_claude_turn(self) -> None:
        self.project("echo check-ran")
        os.environ["FAKE_SESSION"] = "edit-hang"
        codex = self.new("codex")
        sessions.send(codex, "Change it", "send-0001")
        self.started(codex)
        edited = Path(tasks._read(self.home / ".hearth/tasks" / codex)["worktree"]) / "edited.txt"
        for _ in range(200):
            if edited.exists():
                break
            time.sleep(0.05)
        self.assertTrue(edited.exists())
        sessions.stop(codex, "01-session-codex")
        os.environ["FAKE_SESSION"] = "edit"
        claude = self.new("claude")
        sessions.send(claude, "Change it", "send-0002")
        self.settled(claude)
        time.sleep(0.5)

        for session_id in (codex, claude):
            self.assertEqual([run["role"] for run in tasks._read(self.home / ".hearth/tasks" / session_id)["runs"]], ["session"])
        self.assertFalse(any(part.startswith("developer_instructions=") for part in self.calls()[1]["argv"]))
        self.assertEqual(self.calls()[1]["prompt"], "Change it")

    def test_the_check_runs_with_the_backends_environment_and_the_runs_marker_not_as_an_agents_shell(self) -> None:
        self.project("env")
        os.environ["FAKE_SESSION"] = "edit"
        session_id = self.new("codex")
        sessions.send(session_id, "Change it", "send-0001")
        record = self.check_record(session_id)

        lines = (self.home / ".hearth/tasks" / session_id / "runs" / record["dir"] / "output.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn(f"{tasks.RUN_MARKER}={record['marker']}", lines)
        self.assertFalse(any(line.startswith(("HEARTH_TASK=", "CODEX_HOME=")) for line in lines))

    def test_no_check_after_a_failed_turn(self) -> None:
        self.project("echo check-ran")
        os.environ["FAKE_SESSION"] = "fail"
        session_id = self.new("codex")
        sessions.send(session_id, "Change it", "send-0001")
        self.settled(session_id)
        time.sleep(0.5)

        self.assertEqual([run["role"] for run in tasks._read(self.home / ".hearth/tasks" / session_id)["runs"]], ["session"])

    def test_a_turn_settles_and_its_check_runs_though_projects_json_breaks_during_it(self) -> None:
        self.project("echo check-ran")
        os.environ["FAKE_SESSION"] = "edit-go"
        session_id = self.new("codex")
        sessions.send(session_id, "Change it", "send-0001")
        (self.home / ".hearth/projects.json").write_text("{half written", encoding="utf-8")
        (self.home / ".fake-go").touch()
        record = self.check_record(session_id)

        runs = tasks._read(self.home / ".hearth/tasks" / session_id)["runs"]
        self.assertTrue(runs[0]["finished"])
        self.assertEqual((record["command"], record["outcome"]), ("echo check-ran", "done"))

    def test_the_commands_note_names_no_turn_check_when_the_project_has_none(self) -> None:
        self.project("echo check-ran", turn_check=None)
        session_id = self.new("codex")
        sessions.send(session_id, "Hi", "send-0001")
        self.settled(session_id)

        commands = sessions.detail(session_id)["commands"]
        self.assertEqual((commands["turn_check"], commands["host_check"]), (None, "echo check-ran"))
        told = (self.home / ".hearth/tasks" / session_id / "runs/01-session-codex/developer_instructions.md").read_text(encoding="utf-8")
        self.assertNotIn("For your own checks", told)

    def test_the_task_reviewer_and_verifier_argv_stay_unchanged(self) -> None:
        tables = json.dumps([tasks.PROVIDERS, tasks.REVIEWERS, tasks.VERIFIERS])
        self.project("echo check-ran")
        os.environ["FAKE_SESSION"] = "edit"
        session_id = self.new("codex")
        sessions.send(session_id, "Change it", "send-0001")
        self.check_record(session_id)

        self.assertEqual(json.dumps([tasks.PROVIDERS, tasks.REVIEWERS, tasks.VERIFIERS]), tables)
        self.assertNotIn("developer_instructions", tables)

    def test_a_send_during_the_check_is_refused_and_stop_ends_the_check(self) -> None:
        self.project("sleep 60")
        os.environ["FAKE_SESSION"] = "edit"
        session_id = self.new("codex")
        sessions.send(session_id, "Change it", "send-0001")
        record = self.check_record(session_id, finished=False)

        with self.assertRaises(sessions.SessionError) as refused:
            sessions.send(session_id, "Another", "send-0002")
        self.assertEqual(refused.exception.status, 409)
        self.assertIn("check", str(refused.exception))
        sessions.stop(session_id, record["dir"])

        check = sessions.detail(session_id)["turns"][0]["check"]
        self.assertEqual((check["outcome"], check["stopped_by"]), ("cancelled", "keeper"))
        self.assertFalse(tasks._alive(record["pid"]))
        self.assertEqual(len(self.calls()), 1)

    def test_quitting_during_the_check_ends_it(self) -> None:
        self.project("sleep 60")
        os.environ["FAKE_SESSION"] = "edit"
        session_id = self.new("codex")
        sessions.send(session_id, "Change it", "send-0001")
        record = self.check_record(session_id, finished=False)

        sessions.stop_all()

        check = sessions.detail(session_id)["turns"][0]["check"]
        self.assertEqual((check["outcome"], check["stopped_by"]), ("cancelled", "quit"))
        self.assertFalse(tasks._alive(record["pid"]))
        self.assertEqual(sessions._active, {})

    def test_a_check_due_after_quitting_has_begun_never_starts(self) -> None:
        self.project("touch started-anyway")
        os.environ["FAKE_SESSION"] = "edit"
        session_id = self.new("codex")
        sessions._quitting = True  # As if stop_all has already looked when this turn ends.
        sessions.send(session_id, "Change it", "send-0001")
        record = self.check_record(session_id)

        self.assertEqual((record["outcome"], record["stopped_by"], record["pid"]), ("cancelled", "quit", None))
        self.assertFalse((Path(tasks._read(self.home / ".hearth/tasks" / session_id)["worktree"]) / "started-anyway").exists())
        self.assertEqual(sessions._active, {})

    def test_recovery_ends_a_check_left_by_a_crashed_backend(self) -> None:
        session_id = self.new("codex")
        task_dir = self.home / ".hearth/tasks" / session_id
        orphan = self.orphan()
        task = tasks._read(task_dir)
        task["status"] = "running"
        task["runs"] += [{"dir": "01-session-codex", "role": "session", "provider": "codex", "started": tasks._now(), "finished": tasks._now(),
                          "outcome": "done", "send_id": "send-0001", "message": "Hi"},
                         {"dir": "02-check", "role": "check", "provider": "hearth", "command": "sleep 60", "started": tasks._now(),
                          "finished": None, "pid": orphan, "pid_identity": tasks._identity(orphan)}]
        (task_dir / "runs/02-check").mkdir(parents=True)
        tasks._write(task_dir, task)

        sessions.recover()

        self.assertEqual(sessions.detail(session_id)["turns"][0]["check"]["outcome"], "interrupted")
        self.assertFalse(tasks._alive(orphan))
        self.assertEqual(self.calls(), [])

    def test_the_check_runs_when_git_cannot_say_whether_the_turn_changed_anything(self) -> None:
        self.project("echo check-ran")
        session_id = self.new("codex")
        with mock.patch.object(sessions, "_fingerprint", return_value=None):
            sessions.send(session_id, "Hi", "send-0001")
            record = self.check_record(session_id)

        self.assertEqual(record["outcome"], "done")

    def test_the_fingerprint_sees_every_untracked_file_and_ignores_hearths_own(self) -> None:
        session_id = self.new("codex")
        task = tasks._read(self.home / ".hearth/tasks" / session_id)
        worktree = Path(task["worktree"])
        for number in range(sessions.UNTRACKED_LIMIT + 1):
            (worktree / f"new-{number:03d}.txt").write_text("a", encoding="utf-8")
        before = sessions._fingerprint(task)
        (worktree / ".hearth").mkdir(exist_ok=True)
        (worktree / ".hearth/scratch").write_text("hearth's own", encoding="utf-8")
        self.assertEqual(sessions._fingerprint(task), before)
        (worktree / f"new-{sessions.UNTRACKED_LIMIT:03d}.txt").write_text("b", encoding="utf-8")  # Past the Changes view's limit.
        self.assertNotEqual(sessions._fingerprint(task), before)
        self.assertIsNotNone(before)
