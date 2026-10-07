"""ADR-0035: Hearth's own sign-in to the person's brief account, for read-only mail and a calendar of its own.

Hearth asks for exactly two scopes, `gmail.readonly` and `calendar.app.created`, through the OAuth loopback flow with
PKCE, from an OAuth client in the person's own Google Cloud project. It refuses a consent that grants more or less,
or an account other than the one named, and keeps the client and refresh token in the macOS Keychain only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from . import tasks

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.app.created")
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
PROFILE = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
SERVICE = "hearth-google"
SECRETS = ("client-id", "client-secret", "refresh-token")
SETTINGS = "google.json"
WAIT = 300  # Seconds to wait for the person to finish in the browser.
SAFE = re.compile(r"^[A-Za-z0-9._/~+-]+$")  # Keychain values pass through `security -i`, which splits on spaces and quotes.


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    google = subcommands.add_parser("google", help="Connect Hearth to your brief Google account: read-only mail and a Hearth calendar.")
    actions = google.add_subparsers(dest="google_command", required=True)
    connect = actions.add_parser("connect", help="Sign in through your browser and keep the tokens in the Keychain.")
    connect.add_argument("--account", required=True, help="The Google account to connect; signing in to any other is refused.")
    connect.add_argument("--client", type=Path, help="The Desktop app client JSON from Google Cloud; needed the first time.")
    status = actions.add_parser("status", help="Show which account is connected, without contacting Google.")
    status.add_argument("--json", action="store_true", help="Print the connection as one JSON value.")
    disconnect = actions.add_parser("disconnect", help="Show what would be deleted; --apply deletes the tokens from the Keychain.")
    disconnect.add_argument("--apply", action="store_true", help="Delete the client, the refresh token, and the saved account.")


def run(args: argparse.Namespace) -> int:
    if args.google_command == "status":
        return _status(args.json)
    if tasks.refused_inside_task("connect or disconnect the person's Google account"):
        return 1
    if args.google_command == "disconnect":
        return _disconnect(args.apply)
    return _connect(args.account, args.client)


def _connect(account: str, client_file: Path | None) -> int:
    client = _client(client_file)
    if isinstance(client, str):
        print(f"Not connected: {client}", file=sys.stderr)
        return 1
    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result: dict[str, str] = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802, the name http.server calls.
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result.update({key: values[0] for key, values in query.items() if key in ("code", "state", "error")})
            body = b"Hearth received Google's answer. You can close this tab and return to the terminal.\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass  # The request line holds the authorization code.

    server = http.server.HTTPServer(("127.0.0.1", 0), Callback)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_id": client["client-id"], "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "login_hint": account, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Opening Google's sign-in for {account}. If no browser opens, visit:\n{url}", file=sys.stderr)
    webbrowser.open(url)
    deadline, server.timeout = time.monotonic() + WAIT, 5
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    problem = None
    if not result:
        problem = f"no answer from the browser within {WAIT} seconds"
    elif result.get("error"):
        problem = f"Google refused the sign-in: {result['error']}"
    elif not secrets.compare_digest(result.get("state", ""), state):
        problem = "the redirect's state did not match this sign-in, so it may not have come from Google"
    if problem is None:
        try:
            token = _call(TOKEN, {"code": result["code"], "client_id": client["client-id"], "client_secret": client["client-secret"],
                                  "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
            granted = set((token.get("scope") or "").split())
            missing, extra = set(SCOPES) - granted, granted - set(SCOPES)
            if missing or extra:
                problem = (f"Google granted {', '.join(sorted(extra))} beyond what Hearth asked for" if extra
                           else f"the consent left out {', '.join(sorted(missing))}; tick every box Hearth asks for")
            elif not token.get("refresh_token") or not SAFE.match(token["refresh_token"]):
                problem = "Google sent no usable refresh token"
            else:
                email = (_call(PROFILE, token=token.get("access_token")).get("emailAddress") or "").strip()
                if email.casefold() != account.casefold():
                    problem = f"you signed in to {email or 'an unknown account'}, not {account}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            problem = f"Google's answer could not be used ({exc})"
    if problem:
        print(f"Not connected: {problem}; nothing was stored.\nNext: hearth google connect --account {account}", file=sys.stderr)
        return 1

    try:
        for name, value in (("client-id", client["client-id"]), ("client-secret", client["client-secret"]), ("refresh-token", token["refresh_token"])):
            _keychain_set(name, value)
    except (OSError, subprocess.CalledProcessError) as exc:
        for name in SECRETS:
            _keychain_delete(name)
        print(f"Not connected: the Keychain refused the tokens ({exc}); nothing was kept.\nNext: hearth google connect --account {account}",
              file=sys.stderr)
        return 1
    path = tasks._home() / SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"account": email, "scopes": list(SCOPES), "connected_at": tasks._now()}, indent=1), encoding="utf-8")
    print(f"Connected {email} with read-only mail and an app-created calendar; the tokens are in the Keychain.\n"
          "Next: hearth google status", file=sys.stderr)
    return 0


def _client(path: Path | None) -> dict[str, str] | str:
    """The OAuth client from Google's downloaded JSON, or the Keychain; a string says why there is none."""
    if path is None:
        stored = {name: _keychain_get(name) for name in ("client-id", "client-secret")}
        return stored if all(stored.values()) else "no OAuth client is stored yet; pass --client with the JSON you downloaded"
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"the client file could not be read ({exc})"
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        return "the client file is not for a Desktop app; create an OAuth client of type Desktop app in Google Cloud"
    client = {"client-id": installed.get("client_id") or "", "client-secret": installed.get("client_secret") or ""}
    if not all(SAFE.match(value) for value in client.values()):
        return "the client file has no usable client_id and client_secret"
    return client


def _status(as_json: bool) -> int:
    path = tasks._home() / SETTINGS
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    connected = bool(saved.get("account")) and _keychain_get("refresh-token") is not None
    report = {"connected": connected, "account": saved.get("account"), "scopes": saved.get("scopes") or [], "connected_at": saved.get("connected_at")}
    if as_json:
        print(json.dumps(report))
    elif connected:
        print(f"Connected  {report['account']}  since {report['connected_at']}\nScopes     {', '.join(scope.rsplit('/', 1)[-1] for scope in report['scopes'])}")
    else:
        print("Not connected.\nNext: hearth google connect --account <brief account> --client <client JSON>", file=sys.stderr)
    return 0 if connected or as_json else 1


def _disconnect(apply: bool) -> int:
    path = tasks._home() / SETTINGS
    present = [name for name in SECRETS if _keychain_get(name) is not None] + ([path.name] if path.exists() else [])
    if not present:
        print("Nothing to disconnect: no Google tokens are stored.", file=sys.stderr)
        return 0
    if not apply:
        print(f"Would delete {', '.join(present)}.\nNext: hearth google disconnect --apply", file=sys.stderr)
        return 0
    for name in SECRETS:
        _keychain_delete(name)
    path.unlink(missing_ok=True)
    print("Deleted Hearth's Google tokens from the Keychain. Google still lists Hearth until you remove it.\n"
          "Next: remove Hearth at https://myaccount.google.com/connections", file=sys.stderr)
    return 0


def _call(url: str, data: dict | None = None, token: str | None = None) -> dict:
    """One HTTPS request to Google, as a form POST when `data` is given, returning the JSON answer."""
    request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None,
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = json.loads(exc.read() or b"{}") if exc.headers.get_content_type() == "application/json" else {}
        raise ValueError(f"{exc.code} {detail.get('error_description') or detail.get('error') or exc.reason}") from None
    if not isinstance(answer, dict):
        raise ValueError("Google's answer was not a JSON object")
    return answer


def _keychain_set(name: str, value: str) -> None:
    # `security -i` reads the command from standard input, so the secret never appears in the process list.
    subprocess.run(["security", "-i"], input=f"add-generic-password -U -s {SERVICE} -a {name} -w {value}\n",
                   capture_output=True, text=True, check=True)
    if _keychain_get(name) != value:
        raise OSError(f"the Keychain did not keep {name}")


def _keychain_get(name: str) -> str | None:
    found = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"], capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 else None


def _keychain_delete(name: str) -> None:
    subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", name], capture_output=True)
