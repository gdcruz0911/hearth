from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from hearth.cli import main
from hearth.workbench import google

ACCOUNT = "brief@example.com"
CLIENT = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-fake_secret"}}


class GoogleSignInTests(unittest.TestCase):
    """ADR-0035: Hearth signs in to one brief account with exactly two scopes, and keeps its tokens in the Keychain."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary_directory.name)
        self.client = self.home / "client.json"
        self.client.write_text(json.dumps(CLIENT), encoding="utf-8")
        self.keychain: dict[str, str] = {}
        self.calls: list[tuple[str, dict | None, str | None]] = []
        self.granted, self.email, self.state = " ".join(google.SCOPES), ACCOUNT, None
        self.opened: list[str] = []
        patches = [
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(google, "_keychain_set", side_effect=self.keychain.__setitem__),
            mock.patch.object(google, "_keychain_get", side_effect=self.keychain.get),
            mock.patch.object(google, "_keychain_delete", side_effect=lambda name: self.keychain.pop(name, None)),
            mock.patch.object(google, "_call", side_effect=self.fake_google),
            mock.patch.object(google.webbrowser, "open", side_effect=self.fake_browser),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("HEARTH_HOME", None)  # Restored by the patch; a set HEARTH_HOME would move these records.
        os.environ.pop("HEARTH_TASK", None)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def fake_google(self, url: str, data: dict | None = None, token: str | None = None) -> dict:
        self.calls.append((url, data, token))
        if url == google.TOKEN:
            return {"access_token": "access-1", "refresh_token": "1//refresh-1", "scope": self.granted, "expires_in": 3599}
        if url == google.PROFILE:
            return {"emailAddress": self.email}
        raise AssertionError(f"unexpected request to {url}")

    def fake_browser(self, url: str) -> bool:
        """Play the person approving in the browser: Google redirects to Hearth's loopback address with a code."""
        self.opened.append(url)
        query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        redirect = urlsplit(query["redirect_uri"])
        state = self.state if self.state is not None else query["state"]

        def visit() -> None:
            connection = http.client.HTTPConnection(redirect.hostname, redirect.port, timeout=10)
            connection.request("GET", f"/?code=code-1&state={state}")
            connection.getresponse().read()
            connection.close()

        threading.Thread(target=visit, daemon=True).start()
        return True

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main(list(argv))
        return status, output.getvalue(), errors.getvalue()

    def connect(self) -> tuple[int, str, str]:
        return self.run_cli("google", "connect", "--client", str(self.client), "--account", ACCOUNT)

    def settings(self) -> Path:
        return self.home / ".hearth" / google.SETTINGS

    def test_connect_asks_for_exactly_two_scopes_with_pkce_and_keeps_the_tokens_in_the_keychain(self) -> None:
        status, _, said = self.connect()

        self.assertEqual(status, 0, said)
        query = {key: values[0] for key, values in parse_qs(urlsplit(self.opened[0]).query).items()}
        self.assertEqual(set(query["scope"].split()), set(google.SCOPES))
        self.assertEqual((query["access_type"], query["prompt"], query["code_challenge_method"], query["login_hint"]),
                         ("offline", "consent", "S256", ACCOUNT))
        self.assertEqual(urlsplit(query["redirect_uri"]).hostname, "127.0.0.1")
        exchange = next(data for url, data, _ in self.calls if url == google.TOKEN)
        verifier = exchange["code_verifier"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual((query["code_challenge"], exchange["code"], exchange["redirect_uri"]), (challenge, "code-1", query["redirect_uri"]))
        self.assertEqual(self.keychain, {"client-id": CLIENT["installed"]["client_id"], "client-secret": CLIENT["installed"]["client_secret"],
                                         "refresh-token": "1//refresh-1"})
        saved = json.loads(self.settings().read_text(encoding="utf-8"))
        self.assertEqual(saved["account"], ACCOUNT)
        self.assertNotIn("refresh", self.settings().read_text(encoding="utf-8"))
        self.assertIn(f"Connected {ACCOUNT}", said)

    def assert_nothing_stored(self) -> None:
        self.assertEqual((self.keychain, self.settings().exists()), ({}, False))

    def test_a_redirect_with_the_wrong_state_is_refused(self) -> None:
        self.state = "forged"

        status, _, said = self.connect()

        self.assertEqual(status, 1)
        self.assertIn("state did not match", said)
        self.assertNotIn(google.TOKEN, [url for url, _, _ in self.calls])
        self.assert_nothing_stored()

    def test_a_consent_that_left_out_a_scope_is_refused(self) -> None:
        self.granted = google.SCOPES[0]

        status, _, said = self.connect()

        self.assertEqual(status, 1)
        self.assertIn("calendar.app.created", said)
        self.assert_nothing_stored()

    def test_a_consent_with_an_extra_scope_is_refused(self) -> None:
        self.granted = " ".join(google.SCOPES) + " https://www.googleapis.com/auth/gmail.send"

        status, _, said = self.connect()

        self.assertEqual(status, 1)
        self.assertIn("gmail.send", said)
        self.assert_nothing_stored()

    def test_signing_in_to_a_different_account_is_refused(self) -> None:
        self.email = "main@example.com"

        status, _, said = self.connect()

        self.assertEqual(status, 1)
        self.assertIn("main@example.com, not brief@example.com", said)
        self.assert_nothing_stored()

    def test_a_web_client_is_refused_before_any_browser_opens(self) -> None:
        self.client.write_text(json.dumps({"web": CLIENT["installed"]}), encoding="utf-8")

        status, _, said = self.connect()

        self.assertEqual((status, self.opened), (1, []))
        self.assertIn("Desktop app", said)
        self.assert_nothing_stored()

    def test_an_agent_inside_a_task_cannot_connect(self) -> None:
        os.environ["HEARTH_TASK"] = "t-1"

        status, _, said = self.connect()

        self.assertEqual((status, self.opened), (1, []))
        self.assertIn("Agents cannot", said)
        self.assert_nothing_stored()

    def test_status_reports_the_connection_as_json(self) -> None:
        self.assertEqual(json.loads(self.run_cli("google", "status", "--json")[1]), {"connected": False, "account": None, "scopes": [], "connected_at": None})
        self.connect()

        status, shown, _ = self.run_cli("google", "status", "--json")

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(shown) | {"connected_at": None}, {"connected": True, "account": ACCOUNT, "scopes": list(google.SCOPES),
                                                                     "connected_at": None})

    def test_disconnect_previews_and_only_apply_deletes_the_tokens(self) -> None:
        self.connect()

        self.assertEqual(self.run_cli("google", "disconnect")[0], 0)
        self.assertEqual(len(self.keychain), 3)

        status, _, said = self.run_cli("google", "disconnect", "--apply")

        self.assertEqual(status, 0)
        self.assert_nothing_stored()
        self.assertIn("myaccount.google.com", said)


if __name__ == "__main__":
    unittest.main()
