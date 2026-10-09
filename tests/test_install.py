from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from hearth import install

# The Python this suite runs on, or the base it was made from when it runs in a virtual environment.
BASE = sys.executable if sys.prefix == sys.base_prefix else sys._base_executable

# A PEP 517 backend in the stand-in project itself, so building it needs neither setuptools nor the network.
BACKEND = '''
import base64, hashlib, pathlib, zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name = "hearth-0.0.1-py3-none-any.whl"
    files = {str(path): path.read_bytes() for path in pathlib.Path("hearth").rglob("*") if path.is_file()}
    files["hearth-0.0.1.dist-info/METADATA"] = b"Metadata-Version: 2.1\\nName: hearth\\nVersion: 0.0.1\\n"
    files["hearth-0.0.1.dist-info/WHEEL"] = b"Wheel-Version: 1.0\\nGenerator: stand-in\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"
    record = "".join(f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}\\n"
                     for path, data in files.items())
    with zipfile.ZipFile(pathlib.Path(wheel_directory) / name, "w") as wheel:
        for path, data in files.items():
            wheel.writestr(path, data)
        wheel.writestr("hearth-0.0.1.dist-info/RECORD", record + "hearth-0.0.1.dist-info/RECORD,,\\n")
    return name
'''

# Just enough of `hearth web --desktop` for the installer's check: the handshake, the challenge, and the dashboard's script.
CLI = '''
import hashlib, hmac, json, os, pathlib, secrets, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit
import standin_dep

TOKEN, COOKIE = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
SCRIPT = pathlib.Path(__file__).with_name("web_assets") / "dashboard.js"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/desktop/challenge":
            body = json.dumps({"answer": hmac.new(TOKEN.encode(), b"hearth-desktop-challenge\\0" + parse_qs(url.query)["c"][0].encode(),
                                                   hashlib.sha256).hexdigest()}).encode()
        elif url.path == "/assets/dashboard.js" and f"={COOKIE}" in self.headers.get("Cookie", "") and SCRIPT.exists():
            body = SCRIPT.read_bytes()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

class Server(HTTPServer):
    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)

server = Server(("127.0.0.1", 0), Handler)
fd = int(sys.argv[sys.argv.index("--handshake-fd") + 1])
with os.fdopen(fd, "w") as pipe:
    pipe.write(json.dumps({"port": server.server_address[1], "cookie_name": "hearth_x", "cookie": COOKIE, "token": TOKEN}) + "\\n")
server.serve_forever()
'''


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class RuntimeInstallTests(unittest.TestCase):
    """ADR-0038: a released, non-editable runtime with pinned dependencies and a recorded base interpreter."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        self.repo = self.root / "project"
        (self.repo / "hearth/web_assets").mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "pyproject.toml").write_text('[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n\n'
                                                  '[project]\nname = "hearth"\nversion = "0.0.1"\n', encoding="utf-8")
        (self.repo / "backend.py").write_text(BACKEND, encoding="utf-8")
        (self.repo / "hearth/__init__.py").write_text('RELEASE = "committed"\n', encoding="utf-8")
        (self.repo / "hearth/cli.py").write_text(CLI, encoding="utf-8")
        (self.repo / "hearth/web_assets/dashboard.js").write_text("// dashboard\n", encoding="utf-8")
        (self.repo / install.LOCK).write_text(f"standin-dep==1.0 --hash=sha256:{self.dependency()}\n", encoding="utf-8")
        self.commit()
        self.runtimes = self.root / "runtime"

    def dependency(self, contents: str = "VALUE = 1\n") -> str:
        """A pinned dependency wheel in a local folder, standing in for PyPI; returns its hash."""
        path = self.wheels / "standin_dep-1.0-py3-none-any.whl"
        files = {"standin_dep.py": contents.encode(), "standin_dep-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: standin-dep\nVersion: 1.0\n",
                 "standin_dep-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
        record = "".join(f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}\n"
                         for name, data in files.items())
        with zipfile.ZipFile(path, "w") as wheel:
            for name, data in files.items():
                wheel.writestr(name, data)
            wheel.writestr("standin_dep-1.0.dist-info/RECORD", record + "standin_dep-1.0.dist-info/RECORD,,\n")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def commit(self) -> None:
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.email=p@e", "-c", "user.name=P", "commit", "-q", "-m", "release")

    def install(self, base: str = BASE) -> Path:
        return install.install(self.repo, "main", base, self.runtimes, ("--no-index", "--find-links", str(self.wheels)))

    def test_a_runtime_holds_the_committed_release_and_records_its_base_interpreter(self) -> None:
        (self.repo / "hearth/__init__.py").write_text('RELEASE = "uncommitted edit"\n', encoding="utf-8")

        runtime = self.install()

        record = json.loads((runtime / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(record["base"], {"path": BASE, "version": ".".join(map(str, sys.version_info[:3]))})
        self.assertEqual(record["commit"], git(self.repo, "rev-parse", "HEAD").strip())
        self.assertEqual((self.runtimes / "current").resolve(), runtime.resolve())
        shown = subprocess.run([str(runtime / "bin/python"), "-c", "import hearth; print(hearth.RELEASE, hearth.__file__)"],
                               capture_output=True, text=True, check=True, env=install.clean_environment()).stdout
        self.assertTrue(shown.startswith("committed "), shown)
        self.assertTrue(Path(shown.split()[1]).resolve().is_relative_to(runtime.resolve()), shown)  # Installed, not a link to the checkout.

    def test_a_virtual_environment_is_refused_as_the_base(self) -> None:
        subprocess.run([BASE, "-m", "venv", "--without-pip", str(self.root / "venv")], check=True)

        with self.assertRaisesRegex(install.InstallError, "virtual environment"):
            self.install(str(self.root / "venv/bin/python"))
        self.assertFalse(self.runtimes.exists())

    def test_a_dependency_that_does_not_match_its_pinned_hash_installs_nothing(self) -> None:
        self.dependency("VALUE = 'tampered'\n")

        with self.assertRaisesRegex(install.InstallError, "(?i)hash"):
            self.install()
        self.assertEqual([path.name for path in self.runtimes.iterdir()], [])

    def test_a_backend_that_cannot_serve_the_dashboard_is_never_made_current(self) -> None:
        first = self.install()
        (self.repo / "hearth/web_assets/dashboard.js").unlink()  # Package data left out of the release.
        self.commit()

        with self.assertRaisesRegex(install.InstallError, "web assets are missing"):
            self.install()
        self.assertEqual((self.runtimes / "current").resolve(), first.resolve())
        self.assertEqual(sorted(path.name for path in self.runtimes.iterdir()), sorted(["current", first.name]))

    def test_a_ref_without_a_lock_is_refused(self) -> None:
        git(self.repo, "rm", "-q", install.LOCK)
        self.commit()

        with self.assertRaisesRegex(install.InstallError, "has no runtime.lock"):
            self.install()

    def test_the_environment_never_points_the_runtime_at_other_code(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"PYTHONPATH": "/elsewhere", "VIRTUAL_ENV": "/venv", "PYTHONHOME": "/home"}):
            cleaned = install.clean_environment()
        self.assertFalse({"PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME"} & set(cleaned))
        self.assertEqual(cleaned["PYTHONNOUSERSITE"], "1")


if __name__ == "__main__":
    unittest.main()
