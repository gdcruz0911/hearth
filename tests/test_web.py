from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from hearth.service import HearthService
from hearth.web import HearthWebServer


class HearthWebServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "hearth.sqlite"
        self.note = self.root / "facts.md"
        self.note.write_text("# Operations\n\nThe deployment owner is Ada.\n", encoding="utf-8")
        self.selected_file = self.note
        self.selected_directory = self.root
        self.service = HearthService(self.database)
        self.opened_urls: list[str] = []
        self.server = HearthWebServer(
            self.service,
            port=0,
            choose_file=lambda: self.selected_file,
            choose_directory=lambda: self.selected_directory,
            browser_opener=self.opened_urls.append,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._json_request("GET", "api/health")

    def tearDown(self) -> None:
        self.server.close()
        self.thread.join(timeout=2)
        self.service.close()
        self.temporary_directory.cleanup()

    def test_serves_a_capability_scoped_loopback_interface_without_cors(self) -> None:
        root_response, root_body = self._request("GET", self._path())
        missing_token_response, _ = self._request("GET", "/")
        bad_host_response, _ = self._request("GET", self._path(), host="example.test")

        self.assertEqual(root_response.status, 200)
        self.assertIn(b"Your private knowledge base", root_body)
        self.assertEqual(missing_token_response.status, 404)
        self.assertEqual(bad_host_response.status, 400)
        self.assertEqual(root_response.getheader("Cache-Control"), "no-store")
        self.assertIn("default-src 'self'", root_response.getheader("Content-Security-Policy"))
        self.assertEqual(root_response.getheader("Referrer-Policy"), "no-referrer")
        self.assertIsNone(root_response.getheader("Access-Control-Allow-Origin"))

    def test_import_inspection_and_search_keep_source_paths_out_of_normal_payloads(self) -> None:
        imported = self._json_request("POST", "api/import")
        documents = self._json_request("GET", "api/documents")
        inspection = self._json_request("GET", "api/documents/1")
        answer = self._json_request("POST", "api/search", {"question": "Who is the deployment owner?"})

        self.assertEqual(imported["import"]["document"]["name"], "facts.md")
        self.assertEqual(documents["documents"][0]["id"], 1)
        self.assertEqual(inspection["document"]["name"], "facts.md")
        self.assertEqual(inspection["pages"][0]["section"], "Operations")
        self.assertEqual(answer["answer"]["status"], "supported")
        self.assertIn("Ada", answer["answer"]["text"])
        rendered = json.dumps({"import": imported, "documents": documents, "inspection": inspection})
        self.assertNotIn(str(self.note), rendered)
        self.assertNotIn("The deployment owner is Ada.", rendered)
        self.assertTrue(self.note.is_file())

    def test_previewed_rename_requires_one_apply_and_preserves_search(self) -> None:
        self._json_request("POST", "api/import")
        preview = self._json_request(
            "POST",
            "api/documents/1/actions/organize/preview",
            {"operation": "rename", "rename": "owners.md"},
        )["preview"]

        self.assertIn(str(self.note), preview["source_path"])
        self.assertTrue(self.note.is_file())
        applied = self._json_request("POST", f"api/previews/{preview['id']}/apply")["applied"]
        reused_response, reused = self._request("POST", self._path(f"api/previews/{preview['id']}/apply"))
        answer = self._json_request("POST", "api/search", {"question": "Who is the deployment owner?"})

        self.assertEqual(applied["action"], "organize")
        self.assertFalse(self.note.exists())
        self.assertTrue((self.root / "owners.md").is_file())
        self.assertEqual(reused_response.status, 409)
        self.assertIn(b"Preview the action again", reused)
        self.assertEqual(answer["answer"]["citations"][0]["document_name"], "owners.md")

    def test_native_selection_drives_move_relink_and_reindex_previews(self) -> None:
        self._json_request("POST", "api/import")
        destination = self.root / "organized"
        destination.mkdir()
        self.selected_directory = destination

        move_preview = self._json_request(
            "POST", "api/documents/1/actions/organize/preview", {"operation": "move"}
        )["preview"]
        self._json_request("POST", f"api/previews/{move_preview['id']}/apply")
        moved_note = destination / "facts.md"
        self.assertTrue(moved_note.is_file())

        moved_note.write_text("# Operations\n\nThe deployment owner is Lin.\n", encoding="utf-8")
        reindex_preview = self._json_request("POST", "api/documents/1/actions/reindex/preview")["preview"]
        reindexed = self._json_request("POST", f"api/previews/{reindex_preview['id']}/apply")["applied"]
        refreshed_answer = self._json_request("POST", "api/search", {"question": "Who is the deployment owner?"})
        self.assertEqual(reindexed["action"], "reindex")
        self.assertIn("Lin", refreshed_answer["answer"]["text"])

        replacement = self.root / "relocated.md"
        moved_note.rename(replacement)
        self.selected_file = replacement
        relink_preview = self._json_request("POST", "api/documents/1/actions/relink/preview")["preview"]
        relinked = self._json_request("POST", f"api/previews/{relink_preview['id']}/apply")["applied"]

        self.assertEqual(relinked["action"], "relink")
        self.assertEqual(relinked["replacement_source_path"], str(replacement.resolve()))
        self.assertEqual(
            self._json_request("POST", "api/search", {"question": "Who is the deployment owner?"})["answer"]["citations"][0]["document_name"],
            "relocated.md",
        )

    def test_previewed_record_removal_leaves_the_source_file_untouched(self) -> None:
        self._json_request("POST", "api/import")
        preview = self._json_request("POST", "api/documents/1/actions/remove/preview")["preview"]
        applied = self._json_request("POST", f"api/previews/{preview['id']}/apply")["applied"]
        documents = self._json_request("GET", "api/documents")

        self.assertEqual(applied["action"], "remove")
        self.assertTrue(self.note.is_file())
        self.assertEqual(documents["documents"], [])

    def test_open_browser_uses_the_capability_url(self) -> None:
        self.assertIsNone(self.server.open_browser())
        self.assertEqual(self.opened_urls, [self.server.url])

    def _path(self, suffix: str = "") -> str:
        return f"{urlsplit(self.server.url).path}{suffix}"

    def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None, host: str | None = None
    ) -> tuple[http.client.HTTPResponse, bytes]:
        parsed = urlsplit(self.server.url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        encoded_payload = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if encoded_payload is not None else {}
        if host is None:
            connection.request(method, path, body=encoded_payload, headers=headers)
        else:
            connection.putrequest(method, path, skip_host=True)
            connection.putheader("Host", host)
            if encoded_payload is not None:
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(encoded_payload)))
            connection.endheaders(encoded_payload)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def _json_request(self, method: str, endpoint: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        response, body = self._request(method, self._path(endpoint), payload)
        self.assertEqual(response.status, 200, body.decode("utf-8"))
        return json.loads(body)


if __name__ == "__main__":
    unittest.main()
