"""A stand-in for `hearth web --desktop` that logs every request it receives, so tests can see exactly what the app sent.

FAKE_BACKEND_LOG names a JSON-lines file: the first line holds this backend's pid, port, and secrets, then one line per
request with its path and headers. FAKE_BACKEND_MODE "wrong-answer" answers the challenge with the wrong secret, and
"rebuilding" reports a running index rebuild and, on SIGTERM, takes a second to release it, as the real backend waits;
"status-hangs" answers the rebuild status only after six seconds, longer than the app waits.
"""

from __future__ import annotations

import faulthandler

faulthandler.dump_traceback_later(5)  # Shows where startup is stuck if it takes this long, as on CI (2026-10-09).

import hashlib
import hmac
import json
import os
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOG = os.environ["FAKE_BACKEND_LOG"]
lock = threading.Lock()


def log(entry: dict) -> None:
    with lock, open(LOG, "a", encoding="utf-8") as file:
        file.write(json.dumps(entry) + "\n")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        log({"path": self.path, "headers": dict(self.headers.items())})
        if url.path == "/desktop/challenge":
            key = secrets.token_urlsafe(32) if os.environ.get("FAKE_BACKEND_MODE") == "wrong-answer" else TOKEN
            challenge = parse_qs(url.query)["c"][0]
            return self.reply(200, "application/json", json.dumps(
                {"answer": hmac.new(key.encode(), b"hearth-desktop-challenge\0" + challenge.encode(), hashlib.sha256).hexdigest()}))
        if url.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", parse_qs(url.query)["to"][0])
            self.end_headers()
            return None
        if url.path == "/api/semantic-index" and os.environ.get("FAKE_BACKEND_MODE") == "rebuilding":
            return self.reply(200, "application/json", json.dumps({"job": {"status": "running"}}))
        if url.path == "/api/semantic-index" and os.environ.get("FAKE_BACKEND_MODE") == "status-hangs":
            time.sleep(6)
            return self.reply(200, "application/json", json.dumps({"job": {"status": "idle"}}))
        if url.path == "/api/semantic-index":
            return self.reply(200, "application/json", json.dumps({"job": {"status": "idle"}}))  # As the real backend says.
        if url.path == "/":
            return self.reply(200, "text/html; charset=utf-8", "<!doctype html><title>Fake Hearth</title><p>fake dashboard</p>")
        return self.reply(200, "application/json", "{}")

    def reply(self, status: int, kind: str, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = server.server_port
COOKIE, TOKEN = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
log({"pid": os.getpid(), "port": PORT, "cookie": COOKIE, "token": TOKEN})
with os.fdopen(3, "w", encoding="utf-8") as pipe:
    pipe.write(json.dumps({"port": PORT, "cookie_name": f"hearth_{PORT}", "cookie": COOKIE, "token": TOKEN}) + "\n")
print("fake backend running", flush=True)
faulthandler.cancel_dump_traceback_later()


def stop_when_orphaned(parent: int) -> None:
    """Like the real backend: once the app that started this one has gone, stop."""
    while os.getppid() == parent:
        time.sleep(0.2)
    os._exit(0)


def release_and_stop(signum: int, frame: object) -> None:
    if os.environ.get("FAKE_BACKEND_MODE") == "rebuilding":
        time.sleep(1)
        log({"released": time.time()})
    os._exit(0)


signal.signal(signal.SIGTERM, release_and_stop)
threading.Thread(target=stop_when_orphaned, args=(os.getppid(),), daemon=True).start()
try:
    server.serve_forever()
except KeyboardInterrupt:
    sys.exit(0)
