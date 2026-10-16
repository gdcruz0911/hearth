"""Rehearse an update, a crash and its recovery, a rollback, and its undo on copies of the person's data (D4, part 6).

Run from the checkout, with the Hearth app quit: `.venv/bin/python scripts/rehearse_update.py [--ref <commit>]`.
Everything happens under a new ~/hearth-rehearsal-<stamp> folder, which is kept as evidence. The real database, the real
runtime's `current`, and the real settings and task records are checked after every step, and the run stops at the first
step that changed any of them. Only the embedding model is real, and it is only read.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from hearth import install, update  # noqa: E402
from hearth.runtime import load_runtime_profile  # noqa: E402

PYTHON = REPO / ".venv/bin/python"
BASE = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
REAL_HOME = Path.home() / ".hearth"
REAL_PROFILE = Path.home() / "Library/Application Support/Hearth/hearth.json"
OLD = "v0.2.0"


class Failed(Exception):
    pass


def check(condition: bool, what: str) -> None:
    if not condition:
        raise Failed(what)


class Rehearsal:
    def __init__(self, ref: str) -> None:
        self.ref = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", f"{ref}^{{commit}}"], capture_output=True, text=True, check=True).stdout.strip()
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.scratch = Path.home() / f"hearth-rehearsal-{stamp}"
        self.home, self.data, self.root = self.scratch / "hearth", self.scratch / "data", self.scratch / "runtime"
        real = json.loads(REAL_PROFILE.read_text(encoding="utf-8"))
        self.real_database, self.model, self.real_index = Path(real["database"]), real["embedding_model"], Path(real["index_directory"])
        self.real_current = Path(install.ROOT) / "current"
        self.real = self.real_state()

    # The real data, checked after every step.
    def real_state(self) -> tuple:
        records = sorted([*REAL_HOME.glob("*.json"), *REAL_HOME.glob("tasks/*/task.json")])
        records_digest = hashlib.sha256(b"".join(path.name.encode() + path.read_bytes() for path in records if path.is_file())).hexdigest()
        return (hashlib.sha256(self.real_database.read_bytes()).hexdigest(), os.readlink(self.real_current), records_digest)

    def real_unchanged(self, step: str) -> None:
        now = self.real_state()
        for name, before, after in zip(("database file", "runtime current", "settings and task records"), self.real, now):
            check(before == after, f"after {step}, the real {name} changed: {before[:16]} -> {after[:16]}")

    # Every command runs on the scratch home and runtime root, with no agent and no real launcher reachable.
    def env(self) -> dict[str, str]:
        return {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin", "HEARTH_HOME": str(self.home), "PYTHONNOUSERSITE": "1"}

    def install(self, *argv: str, popen: bool = False):
        if argv[0] == "recover":  # It takes its runtime root from the marker, which must name the scratch one.
            check(Path(json.loads((self.home / update.MARKER).read_text(encoding="utf-8"))["root"]) == self.root, "the marker names another runtime root")
        else:
            check("--root" in argv and Path(argv[argv.index("--root") + 1]) == self.root, f"hearth.install {argv[0]} without the scratch --root")
        command = [str(PYTHON), "-m", "hearth.install", "--repo", str(REPO), *argv]
        if popen:
            return subprocess.Popen(command, env=self.env(), cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
        return subprocess.run(command, env=self.env(), cwd=REPO, capture_output=True, text=True, timeout=1800)

    def current(self) -> str:
        return os.readlink(self.root / "current")

    def digest(self) -> str:
        """The scratch database's content; a restore through SQLite rewrites pages, so its file bytes may differ."""
        connection = sqlite3.connect(f"{(self.data / 'hearth.sqlite').as_uri()}?mode=ro", uri=True)
        try:
            return hashlib.sha256("\n".join(connection.iterdump()).encode()).hexdigest()
        finally:
            connection.close()

    def memory_is_free(self) -> None:
        level = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True, text=True).stdout.strip()
        check(level == "1", f"memory pressure is {level}, not 1; run again when the Mac is idle")

    def backend_starts(self) -> None:
        """The scratch `current` backend starts on the scratch data as the app would start it, and answers its handshake."""
        self.memory_is_free()
        ours, theirs = socket.socketpair()
        backend = subprocess.Popen([str(self.root / "current/bin/python"), "-I", "-m", "hearth.cli", "--profile", str(self.home / "profile.json"),
                                    "web", "--desktop", "--handshake-fd", str(theirs.fileno())], pass_fds=(theirs.fileno(),),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=self.env(), cwd=Path.home())
        theirs.close()
        try:
            install.check_handshake(ours, backend)
        finally:
            ours.close()
            update._stop(backend)

    # Steps.
    def copy(self) -> None:
        check(not list(REAL_HOME.glob("tasks/*/resume.json")) and not list(REAL_HOME.glob("tasks/*/refused-*.json")),
              "the real home has live or refused continuations, which an update would resume against real projects")
        self.data.mkdir(parents=True)
        source = sqlite3.connect(f"{self.real_database.as_uri()}?mode=ro", uri=True)
        copy = sqlite3.connect(self.data / "hearth.sqlite")
        try:
            source.backup(copy)
        finally:
            source.close()
            copy.close()
        shutil.copytree(self.real_index, self.data / "indexes/main")
        left_out = {*update.SKIPPED, "profile.json"}
        shutil.copytree(REAL_HOME, self.home, symlinks=True, ignore=lambda folder, names: [name for name in names if name in left_out])
        (self.data / "hearth.json").write_text(json.dumps({
            "format": "hearth-runtime-profile-v1", "database": str(self.data / "hearth.sqlite"), "embedding_model": self.model,
            "index_directory": str(self.data / "indexes/main"), "source_roots": [], "recall_roots": []}), encoding="utf-8")
        (self.home / "profile.json").symlink_to(self.data / "hearth.json")
        profile = load_runtime_profile(self.home / "profile.json")
        for name in ("database", "index_directory"):
            check(getattr(profile, name).resolve().is_relative_to(self.scratch), f"the scratch profile's {name} is outside {self.scratch}")
        check(profile.source_roots == () and profile.recall_roots == (), "the scratch profile names source or recall folders")
        check(str(profile.embedding_model) == self.model, "the scratch profile names another model")
        for path in self.home.rglob("*"):
            if path.is_symlink():
                check(path.resolve().is_relative_to(self.scratch), f"{path} links outside the scratch folder, to {path.resolve()}")
        print(f"  copied the database ({self.real_database.stat().st_size // 1024} KB), the index, and {sum(1 for _ in self.home.glob('tasks/*/task.json'))} task records")

    def old_runtime(self) -> None:
        self.memory_is_free()  # Installing starts the new runtime's backend to check its handshake.
        result = self.install("runtime", "--base", BASE, "--ref", OLD, "--root", str(self.root))
        check(result.returncode == 0, f"installing {OLD} failed:\n{result.stderr[-2000:]}")
        print(f"  current -> {self.current()}")

    def refused_while_busy(self) -> None:
        busy = [json.loads(path.read_text(encoding="utf-8"))["id"] for path in self.home.glob("tasks/*/task.json")
                if json.loads(path.read_text(encoding="utf-8"))["status"] in ("running", "queued", "waiting")]
        if not busy:  # Nothing real is waiting: a copied record made waiting, in the scratch copy only, stands in.
            donor = next(self.home.glob("tasks/*/task.json"))
            busy = ["20991231-235800"]
            (self.home / "tasks" / busy[0]).mkdir()
            (self.home / "tasks" / busy[0] / "task.json").write_text(
                json.dumps({**json.loads(donor.read_text(encoding="utf-8")), "id": busy[0], "status": "waiting", "stop_reason": "question"}), encoding="utf-8")
        before, current = self.digest(), self.current()
        self.memory_is_free()  # Building the new runtime starts its backend to check its handshake.
        result = self.install("update", "--ref", self.ref, "--root", str(self.root))
        check(result.returncode == 1 and all(f"{task} is" in result.stderr for task in busy), f"the update was not refused for {busy}:\n{result.stderr[-2000:]}")
        check((self.digest(), self.current()) == (before, current) and not (self.home / update.BACKUPS).exists(), "the refused update changed something")
        print(f"  refused, naming {', '.join(busy)}; built the new runtime only")
        for task in busy:  # The copies only: the rest of the rehearsal needs a home with nothing under way.
            shutil.rmtree(self.home / "tasks" / task)

    def crash_and_recover(self) -> None:
        before, current = self.digest(), self.current()
        self.memory_is_free()
        updater = self.install("update", "--ref", self.ref, "--root", str(self.root), popen=True)
        marker = self.home / update.MARKER
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and updater.poll() is None:
            try:
                if json.loads(marker.read_text(encoding="utf-8")).get("step") == "changing":
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        if updater.poll() is not None:  # Read its output only here: communicate() would otherwise wait for it to finish.
            raise Failed(f"the update ended before it could be killed while changing data:\n{updater.communicate()[1][-2000:]}")
        os.killpg(updater.pid, signal.SIGKILL)  # The updater and everything it started, as a crash or a forced quit would leave it.
        updater.communicate()
        time.sleep(0.5)
        left = subprocess.run(["pgrep", "-f", str(self.scratch)], capture_output=True, text=True).stdout.split()
        check(not left, f"processes from the scratch folder outlived the crash: {left}")
        check(marker.exists(), "the crash left no marker")
        writer = subprocess.run([str(PYTHON), "-m", "hearth.cli", "list"], env=self.env(), cwd=REPO, capture_output=True, text=True, timeout=120)
        check(writer.returncode != 0 and "hearth.install recover" in writer.stderr, f"a writer was not stopped after the crash:\n{writer.stderr[-1000:]}")
        result = self.install("recover")
        check(result.returncode == 0 and "undone" in result.stdout, f"recover failed:\n{result.stderr[-2000:]}")
        check((self.digest(), self.current()) == (before, current) and not marker.exists(), "recover did not bring the data and runtime back")
        print(f"  killed while changing data; writers refused; {result.stdout.strip().splitlines()[0]}")

    def clean_update(self) -> None:
        self.memory_is_free()
        result = self.install("update", "--ref", self.ref, "--root", str(self.root))
        check(result.returncode == 0, f"the update failed:\n{result.stderr[-2000:]}")
        installed = json.loads((self.root / "current/runtime.json").read_text(encoding="utf-8"))["commit"]
        check(installed == self.ref, f"current is {self.current()}, built from {installed}")
        self.backend_starts()
        print(f"  current -> {self.current()}; the backend starts on the copied data")

    def rollback_and_undo(self) -> None:
        updated, after_update = self.current(), self.digest()
        # Work done after the update, in the copies only: a new task record and a changed setting.
        donor = next(self.home.glob("tasks/*/task.json"))
        record = {**json.loads(donor.read_text(encoding="utf-8")), "id": "20991231-235900"}
        (self.home / "tasks/20991231-235900").mkdir()
        (self.home / "tasks/20991231-235900/task.json").write_text(json.dumps(record), encoding="utf-8")
        settings = self.home / "projects.json"
        settings_after = settings.read_text(encoding="utf-8") + "\n"
        settings.write_text(settings_after, encoding="utf-8")

        preview = self.install("rollback", "--root", str(self.root))
        check(preview.returncode == 0 and "task 20991231-235900 (made since)" in preview.stdout and "projects.json (changed since)" in preview.stdout,
              f"the preview did not name the work done since:\n{preview.stdout}{preview.stderr[-1000:]}")
        check(self.current() == updated and self.digest() == after_update and (self.home / "tasks/20991231-235900").exists(), "the preview changed something")
        print("  preview:\n" + "\n".join(f"    {line}" for line in preview.stdout.strip().splitlines()))

        result = self.install("rollback", "--root", str(self.root), "--confirm")
        check(result.returncode == 0, f"the rollback failed:\n{result.stderr[-2000:]}")
        check(self.current().startswith(OLD) and not (self.home / "tasks/20991231-235900").exists() and settings.read_text(encoding="utf-8") != settings_after,
              "the rollback did not restore the runtime, the task records, and the settings")
        self.backend_starts()
        print(f"  rolled back: current -> {self.current()}; the {OLD} backend starts on the restored data")

        saved = next(line.split(" --backup ")[1].split()[0] for line in result.stdout.splitlines() if " --backup " in line)
        undo = self.install("rollback", "--root", str(self.root), "--backup", saved, "--confirm")
        check(undo.returncode == 0, f"undoing the rollback failed:\n{undo.stderr[-2000:]}")
        check(self.current() == updated and self.digest() == after_update and (self.home / "tasks/20991231-235900/task.json").exists()
              and settings.read_text(encoding="utf-8") == settings_after, "the undo did not bring back the data from after the update")
        self.backend_starts()
        print(f"  undone with {saved}: current -> {self.current()}; the data from after the update is back")

    def run(self) -> int:
        print(f"Rehearsing {OLD} -> {self.ref} in {self.scratch}")
        steps = [("copy the data", self.copy), (f"install {OLD}", self.old_runtime), ("update refused while busy", self.refused_while_busy),
                 ("crash mid-update, then recover", self.crash_and_recover), ("clean update", self.clean_update),
                 ("rollback, then undo", self.rollback_and_undo)]
        for name, step in steps:
            print(f"- {name}")
            try:
                step()
                self.real_unchanged(name)
            except Failed as exc:
                print(f"FAILED at '{name}': {exc}\nThe scratch folder is kept: {self.scratch}", file=sys.stderr)
                return 1
        print(f"Passed for {self.ref}. The real database, runtime, settings and task records are unchanged.\n"
              f"Evidence: {self.scratch}\nA pass covers only this commit; tag this commit for the real update.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default="origin/main", help="The commit to update to (default: origin/main).")
    args = parser.parse_args()
    return Rehearsal(args.ref).run()


if __name__ == "__main__":
    sys.exit(main())
