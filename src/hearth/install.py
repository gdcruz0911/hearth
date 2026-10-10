"""The Hearth app's managed runtime (ADR-0038): a released, non-editable Hearth in its own Python environment.

`.venv/bin/python -m hearth.install lock --base <python>`, run in the checkout, pins the runtime's dependencies, with hashes, in runtime.lock.
`.venv/bin/python -m hearth.install runtime --base <python> --ref <git ref>` builds a runtime from that ref, never from the working
tree, under ~/Library/Application Support/Hearth/runtime/. It records the base interpreter's path and exact version, so the
app can refuse to start if that Python changes, and it points `current` at the new runtime only after the installed
backend has started and proved its handshake.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import http.client
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path.home() / "Library/Application Support/Hearth/runtime"
LOCK = "runtime.lock"
FORMAT = "hearth-runtime-v1"
READY_SECONDS = 30  # The app's own wait for a backend's handshake.
# Variables that would quietly load other code into the released runtime, such as the working checkout.
SCRUBBED = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__")


class InstallError(RuntimeError):
    """Raised when a runtime cannot be built exactly as asked; nothing is switched."""


def clean_environment(**extra: str) -> dict[str, str]:
    """This process's environment without anything that points Python at code outside the runtime."""
    return {**{key: value for key, value in os.environ.items() if key not in SCRUBBED}, "PYTHONNOUSERSITE": "1", **extra}


def base_python(path: str) -> dict:
    """The base interpreter as given, and its exact version; a virtual environment is refused, since deleting it breaks the app."""
    if not os.path.isabs(path) or not os.access(path, os.X_OK):
        raise InstallError(f"{path} is not an absolute path to a Python interpreter.\n"
                           "Next: pass the python.org framework Python, such as /Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14")
    probe = "import json, platform, sys; print(json.dumps([platform.python_version(), sys.prefix, sys.base_prefix]))"
    version, prefix, base_prefix = json.loads(_run([path, "-I", "-c", probe]).stdout)
    if prefix != base_prefix:
        raise InstallError(f"{path} is a virtual environment, not a base interpreter.\n"
                           "Next: pass the python.org framework Python, such as /Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14")
    return {"path": path, "version": version}


def lock(repo: Path, base: str, uploaded_prior_to: str) -> Path:
    """Resolve the runtime's dependencies for this Mac's Python and write each one's version and hash to runtime.lock."""
    import tomllib

    base_python(base)
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    wanted = [*project["project"]["dependencies"], *project["project"]["optional-dependencies"]["local-inference"],
              *project["build-system"]["requires"]]  # setuptools builds Hearth itself inside the runtime.
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "report.json"
        _run([base, "-I", "-m", "pip", "install", "--dry-run", "--ignore-installed", "--only-binary=:all:", "--quiet",
              f"--uploaded-prior-to={uploaded_prior_to}", "--report", str(report), *wanted])
        installs = json.loads(report.read_text(encoding="utf-8"))["install"]
    lines = [f"# Pinned by `python -m hearth.install lock` for {sys.platform} {os.uname().machine}, Python {base_python(base)['version']}.",
             f"# Packages uploaded before {uploaded_prior_to}; install with --require-hashes --only-binary=:all:."]
    for item in sorted(installs, key=lambda entry: entry["metadata"]["name"].lower()):
        digest = item["download_info"]["archive_info"]["hashes"]["sha256"]
        lines.append(f"{item['metadata']['name']}=={item['metadata']['version']} --hash=sha256:{digest}")
    (repo / LOCK).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return repo / LOCK


class AlreadyInstalled(InstallError):
    """Raised for a complete runtime of the same release on the same Python; `target` is that runtime."""

    def __init__(self, message: str, target: Path):
        super().__init__(message)
        self.target = target


def install(repo: Path, ref: str, base: str, root: Path = ROOT, pip_options: tuple[str, ...] = (), make_current: bool = True) -> Path:
    """Build a runtime from `ref`, check that its backend starts, then make it current unless `make_current` is False; returns its folder."""
    interpreter = base_python(base)
    commit = _run(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"]).stdout.strip()
    # Named for the base Python's version and path too, so rebuilding after that Python is upgraded, or replaced by one at
    # another path, makes a new runtime beside the old one instead of meeting it.
    identity = hashlib.sha256(interpreter["path"].encode()).hexdigest()[:8]
    target = root / f"{''.join(c if c.isalnum() or c in '.-_' else '-' for c in ref)}-{commit[:12]}-py{interpreter['version']}-{identity}"
    if target.exists() and not (target / "runtime.json").is_file() and not (root / "current").resolve() == target.resolve():
        shutil.rmtree(target)  # Left half-built by a run that died; a runtime is complete only once runtime.json is written.
    if target.exists():
        raise AlreadyInstalled(f"A runtime for {ref} at {commit[:12]} on Python {interpreter['version']} is already installed at {target}, from that same interpreter.\n"
                               f"Next: use it, or remove {target} and install again", target)
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as scratch:
            source = Path(scratch) / "source"
            _export(repo, commit, source)
            if not (source / LOCK).is_file():
                raise InstallError(f"{ref} has no {LOCK}, so its dependencies are not pinned.\n"
                                   f"Next: run .venv/bin/python -m hearth.install lock, commit {LOCK}, and install from that commit")
            _run([base, "-I", "-m", "venv", str(target)])
            python = str(target / "bin/python")
            pip = [python, "-I", "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *pip_options]
            # Hash mode refuses an unhashed local project, so the pinned dependencies go first and Hearth on its own after.
            _run([*pip, "--require-hashes", "--only-binary=:all:", "-r", str(source / LOCK)])
            _run([*pip, "--no-deps", "--no-build-isolation", str(source)])
        _check_backend(python)
        (target / "runtime.json").write_text(json.dumps({
            "format": FORMAT, "base": interpreter, "ref": ref, "commit": commit, "source": str(repo.resolve()),  # Where to rebuild it.
            "installed": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        }, indent=2) + "\n", encoding="utf-8")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)  # Never leave a half-built runtime where the app could pick it up.
        raise
    if make_current:
        switch(root / "current", target)
    return target


def _export(repo: Path, commit: str, destination: Path) -> None:
    """The committed tree only: uncommitted edits in the checkout never reach a release."""
    destination.mkdir()
    archive = destination.parent / "source.tar"
    with archive.open("wb") as file:
        subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], stdout=file, check=True)
    with tarfile.open(archive) as tar:
        tar.extractall(destination, filter="data")


def _check_backend(python: str) -> None:
    """Start the installed backend as the app would, and require the handshake, the challenge, and the dashboard's script."""
    with tempfile.TemporaryDirectory() as scratch:
        environment = clean_environment(HEARTH_HOME=f"{scratch}/hearth")  # Never the person's ~/.hearth or their data.
        # The desktop backend reads its data only from <HEARTH_HOME>/profile.json, so the check gets a scratch one, made by the
        # release itself, naming a new, empty knowledge base.
        try:
            made = subprocess.run([python, "-m", "hearth.cli", "profile", "create", f"{scratch}/hearth/profile.json", "--database",
                                   f"{scratch}/check.sqlite", "--create-database"], capture_output=True, text=True, env=environment,
                                  cwd=scratch, timeout=READY_SECONDS)
        except subprocess.TimeoutExpired:
            raise InstallError(f"The installed Hearth did not create a scratch profile within {READY_SECONDS} seconds.") from None
        if made.returncode != 0:
            raise InstallError(f"The installed Hearth could not create a scratch profile:\n{(made.stderr or made.stdout).strip()[-1000:]}")
        ours, theirs = socket.socketpair()  # What the app's Node hands the backend, so the check passes only if the app's would.
        backend = subprocess.Popen([python, "-m", "hearth.cli", "web", "--desktop", "--handshake-fd", str(theirs.fileno())],
                                   pass_fds=(theirs.fileno(),), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment, cwd=scratch)
        theirs.close()
        try:
            check_handshake(ours, backend)
        finally:
            ours.close()
            backend.terminate()
            backend.wait(timeout=30)


def check_handshake(ours: socket.socket, backend: subprocess.Popen) -> None:
    """Require a started backend's handshake on `ours`, a correct challenge answer, and the dashboard's script."""
    ours.settimeout(READY_SECONDS)  # As long as the app waits; a backend that stalls fails here instead of hanging.
    try:
        with ours.makefile(encoding="utf-8") as pipe:
            line = pipe.readline()
    except TimeoutError:
        raise InstallError(f"The installed backend did not start within {READY_SECONDS} seconds.") from None
    if not line:
        raise InstallError(f"The installed backend stopped before its handshake (exit code {backend.wait(timeout=30)}).")
    handshake = json.loads(line)
    challenge = secrets.token_hex(32)
    answer = json.loads(_get(handshake["port"], f"/desktop/challenge?c={challenge}", {})[1])["answer"]
    expected = hmac.new(handshake["token"].encode(), b"hearth-desktop-challenge\0" + challenge.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(answer, expected):
        raise InstallError("The installed backend did not answer its challenge.")
    status, body = _get(handshake["port"], "/assets/dashboard.js", {"Cookie": f"{handshake['cookie_name']}={handshake['cookie']}"})
    if status != 200 or not body:
        raise InstallError(f"The installed backend did not serve the dashboard's script (HTTP {status}), so its web assets are missing.")


def _get(port: int, path: str, headers: dict[str, str]) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def switch(link: Path, target: Path) -> None:
    """Point `current` at the new runtime in one step, so the app sees either the old one or the new one."""
    staged = link.with_name(".current-switching")
    staged.unlink(missing_ok=True)
    staged.symlink_to(target.name)
    staged.replace(link)


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, capture_output=True, text=True, env=clean_environment())
    if result.returncode != 0:
        raise InstallError(f"{' '.join(argv[:4])} ... failed:\n{(result.stderr or result.stdout).strip()[-2000:]}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=".venv/bin/python -m hearth.install", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="The Hearth checkout (default: the current folder).")
    commands = parser.add_subparsers(dest="command", required=True)
    locker = commands.add_parser("lock", help=f"Pin the runtime's dependencies, with hashes, in {LOCK}.")
    locker.add_argument("--base", required=True, help="The base Python the runtime will use.")
    fortnight = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=14)).strftime("%Y-%m-%dT00:00:00Z")
    locker.add_argument("--uploaded-prior-to", default=fortnight, help="Only packages uploaded before this time (default: 14 days ago).")
    runtime = commands.add_parser("runtime", help="Build a runtime from a git ref and make it current.")
    runtime.add_argument("--base", required=True, help="The base Python, such as /Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14.")
    runtime.add_argument("--ref", required=True, help="The commit or tag to release; uncommitted edits are never included.")
    runtime.add_argument("--root", type=Path, default=ROOT, help=f"Where runtimes live (default: {ROOT}).")
    updater = commands.add_parser("update", help="Update the app's runtime to a release, backing up and checking your data first (ADR-0040).")
    updater.add_argument("--ref", required=True, help="The release tag to update to.")
    updater.add_argument("--base", help="The base Python; by default, the one the current runtime was built on.")
    updater.add_argument("--root", type=Path, default=ROOT, help=f"Where runtimes live (default: {ROOT}).")
    commands.add_parser("recover", help="Finish an interrupted update: undo it if it had changed your data, otherwise only clear it.")
    args = parser.parse_args(argv)
    if args.command in ("update", "recover") and os.environ.get("HEARTH_TASK"):
        print("Agents cannot update Hearth; this shell belongs to a task.\nNext: ask the person, through the task board or your final report", file=sys.stderr)
        return 1
    try:
        if args.command == "lock":
            print(f"Wrote {lock(args.repo, args.base, args.uploaded_prior_to)}")
        elif args.command == "update":
            from . import update
            print(update.update(args.repo, args.ref, args.base, args.root))
        elif args.command == "recover":
            from . import update
            print(update.recover())
        else:
            print(f"Installed {install(args.repo, args.ref, args.base, args.root)} and made it current.")
    except InstallError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
