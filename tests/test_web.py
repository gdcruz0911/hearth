from __future__ import annotations

import http.client
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import contextlib
import fcntl
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import urlsplit

from hearth.domain import DocumentRelationship
from hearth.embedding import IndexBuildCancelled, IndexBuildStopped, IndexBusy
from hearth.service import HearthService
from hearth import web
from hearth.web import HearthWebApplication, HearthWebServer


HOST = "127.0.0.1:8765"


def signed_in(app: HearthWebApplication):
    """Redeem the app's launch link as the person's browser would, and return a respond() that sends the session."""
    launch = dict(app.respond("GET", app.launch_path, b"", {"Host": HOST}).headers)
    headers = {"Host": HOST, "Origin": f"http://{HOST}", "Cookie": launch["Set-Cookie"].split(";")[0],
               "X-Hearth-Session": launch["Location"].split("#session=")[1]}
    call = lambda method, path, body=b"": app.respond(method, path, body, headers)
    call.headers = headers
    return call


class FakeRelationshipIndex:
    def __init__(self) -> None:
        self.rebuild_count = 0

    def rebuild(self, chunks, *, on_progress=None, is_cancelled=None, on_warning=None) -> None:
        self.rebuild_count += 1
        if on_progress is not None:
            on_progress(len(chunks), len(chunks))

    def is_current(self, chunks) -> bool:
        return True

    def search(self, question: str, chunks, limit: int = 20):
        return []

    def document_relationships(self, chunks, limit: int = 12, minimum_score: float = 0.72):
        return [DocumentRelationship(chunks[0], chunks[1], 0.91)] if len(chunks) == 2 else []


class BlockingRelationshipIndex(FakeRelationshipIndex):
    def __init__(self) -> None:
        super().__init__()
        self.block_rebuild = False
        self.started = threading.Event()

    def rebuild(self, chunks, *, on_progress=None, is_cancelled=None, on_warning=None) -> None:
        if not self.block_rebuild:
            super().rebuild(chunks, on_progress=on_progress, is_cancelled=is_cancelled)
            return
        self.started.set()
        while is_cancelled is None or not is_cancelled():
            time.sleep(0.01)
        raise IndexBuildCancelled("cancelled in test")


class PressuredRelationshipIndex(BlockingRelationshipIndex):
    def rebuild(self, chunks, *, on_progress=None, is_cancelled=None, on_warning=None) -> None:
        if not self.block_rebuild:
            super().rebuild(chunks, on_progress=on_progress, is_cancelled=is_cancelled)
            return
        raise IndexBuildStopped("Hearth stopped the semantic-index rebuild because macOS reported memory pressure.")


class UnmonitoredRelationshipIndex(BlockingRelationshipIndex):
    def rebuild(self, chunks, *, on_progress=None, is_cancelled=None, on_warning=None) -> None:
        if self.block_rebuild and on_warning is not None:
            on_warning("Hearth could not read macOS memory pressure or swap use, so this rebuild is not protected.")
        FakeRelationshipIndex.rebuild(self, chunks, on_progress=on_progress, is_cancelled=is_cancelled)


class HearthWebServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "hearth.sqlite"
        self.note = self.root / "facts.md"
        self.note.write_text("# Operations\n\nThe deployment owner is Ada.\n", encoding="utf-8")
        self.selected_file = self.note
        self.service = HearthService(self.database)
        self.opened_urls: list[str] = []
        self.server = HearthWebServer(
            self.service,
            port=0,
            choose_file=lambda: self.selected_file,
            browser_opener=self.opened_urls.append,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._sign_in()
        self._json_request("GET", "api/health")

    def tearDown(self) -> None:
        self.server.close()
        self.thread.join(timeout=2)
        self.service.close()
        self.temporary_directory.cleanup()

    def test_a_client_that_keeps_its_connection_open_does_not_starve_another(self) -> None:
        # The dashboard polls every few seconds; on a kept-alive connection it used to block every other request.
        parsed = urlsplit(self.server.url)
        holder = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        holder.request("GET", self._path("api/health"), headers=self._session)
        held = holder.getresponse()
        held.read()
        self.addCleanup(holder.close)
        other = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        self.addCleanup(other.close)

        other.request("GET", self._path("api/documents"), headers=self._session)
        answered = other.getresponse()

        self.assertEqual((held.getheader("Connection"), answered.status), ("close", 200))

    def test_serves_a_capability_scoped_loopback_interface_without_cors(self) -> None:
        root_response, root_body = self._request("GET", self._path())
        self._session = {}
        missing_token_response, _ = self._request("GET", "/api/health")
        bad_host_response, _ = self._request("GET", self._path(), host="example.test")

        self.assertEqual(root_response.status, 200)
        self.assertIn(b"Hearth", root_body)
        self.assertEqual(missing_token_response.status, 401)
        self.assertEqual(bad_host_response.status, 400)
        self.assertEqual(root_response.getheader("Cache-Control"), "no-store")
        self.assertIn("default-src 'self'", root_response.getheader("Content-Security-Policy"))
        self.assertNotRegex(root_response.getheader("Content-Security-Policy"), r"unsafe-inline|unsafe-eval")
        self.assertEqual(root_response.getheader("Referrer-Policy"), "no-referrer")
        self.assertIsNone(root_response.getheader("Access-Control-Allow-Origin"))

    def test_import_inspection_and_search_keep_source_paths_out_of_normal_payloads(self) -> None:
        imported = self._json_request("POST", "api/import")
        documents = self._json_request("GET", "api/documents")
        inspection = self._json_request("GET", "api/documents/1")
        answer = self._json_request("POST", "api/search/report", {"question": "Who is the deployment owner?"})

        self.assertEqual(imported["import"]["document"]["name"], "facts.md")
        self.assertEqual(documents["documents"][0]["id"], 1)
        self.assertEqual(inspection["document"]["name"], "facts.md")
        self.assertEqual(inspection["pages"][0]["section"], "Operations")
        self.assertEqual(answer["status"], "supported")
        self.assertIn("Ada", answer["evidence"][0]["excerpt"])
        rendered = json.dumps({"import": imported, "documents": documents, "inspection": inspection})
        self.assertNotIn(str(self.note), rendered)
        self.assertNotIn("The deployment owner is Ada.", rendered)

    def test_semantic_map_relationship_is_explainable_only_on_request(self) -> None:
        self.server.close()
        self.thread.join(timeout=2)
        self.service.close()
        self.service = HearthService(self.database, semantic_index=FakeRelationshipIndex())
        self.server = HearthWebServer(
            self.service,
            port=0,
            choose_file=lambda: self.selected_file,
            browser_opener=self.opened_urls.append,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._sign_in()
        self._json_request("GET", "api/health")
        self._json_request("POST", "api/import")
        second_note = self.root / "handoff.md"
        second_note.write_text("The handoff owner is Lin.", encoding="utf-8")
        self.selected_file = second_note
        self._json_request("POST", "api/import")

        collection_map = self._json_request("GET", "api/map")
        relationship = self._json_request("GET", "api/relationships/1/2")["relationship"]

        self.assertEqual(collection_map["semantic_edges"], [{"left_document_id": 1, "right_document_id": 2, "score": 0.91}])
        self.assertNotIn("Ada", json.dumps(collection_map))
        self.assertEqual(relationship["score"], 0.91)
        self.assertEqual(len(relationship["evidence"]), 2)
        self.assertIn("Ada", relationship["evidence"][0]["quote"])
        self.assertTrue(self.note.is_file())

    def test_semantic_index_rebuild_requires_a_separate_apply(self) -> None:
        self.server.close()
        self.thread.join(timeout=2)
        self.service.close()
        index = FakeRelationshipIndex()
        self.service = HearthService(self.database, semantic_index=index)
        self.server = HearthWebServer(
            self.service,
            port=0,
            choose_file=lambda: self.selected_file,
            browser_opener=self.opened_urls.append,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._sign_in()
        self._json_request("GET", "api/health")
        self._json_request("POST", "api/import")
        before_preview = index.rebuild_count

        preview = self._json_request("POST", "api/semantic-index/preview")["preview"]

        self.assertEqual(index.rebuild_count, before_preview)
        self.assertEqual(preview["action"], "semantic-index")
        self.assertNotIn(str(self.note), json.dumps(preview))

        applied = self._json_request("POST", f"api/previews/{preview['id']}/apply")["applied"]
        job = self._wait_for_semantic_job({"completed"})

        self.assertEqual(applied["action"], "semantic-index")
        self.assertIn(applied["job"]["status"], {"running", "completed"})
        self.assertEqual(job["semantic_index_status"], "ready")
        self.assertEqual(job["phase"], "ready")
        self.assertGreaterEqual(job["benchmark"]["elapsed_seconds"], 0)
        self.assertGreaterEqual(job["benchmark"]["peak_resident_memory_bytes"], 1)
        self.assertEqual(index.rebuild_count, before_preview + 1)

    def test_semantic_index_rebuild_can_be_cancelled_without_blocking_the_web_server(self) -> None:
        self.server.close()
        self.thread.join(timeout=2)
        self.service.close()
        index = BlockingRelationshipIndex()
        self.service = HearthService(self.database, semantic_index=index)
        self.server = HearthWebServer(
            self.service,
            port=0,
            choose_file=lambda: self.selected_file,
            browser_opener=self.opened_urls.append,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._sign_in()
        self._json_request("GET", "api/health")
        self._json_request("POST", "api/import")
        index.block_rebuild = True
        preview = self._json_request("POST", "api/semantic-index/preview")["preview"]

        applied = self._json_request("POST", f"api/previews/{preview['id']}/apply")["applied"]

        self.assertEqual(applied["job"]["status"], "running")
        self.assertTrue(index.started.wait(timeout=1))
        self.assertEqual(self._json_request("GET", "api/health")["document_count"], 1)
        cancelling = self._json_request("POST", "api/semantic-index/cancel")["job"]
        cancelled = self._wait_for_semantic_job({"cancelled"})

        self.assertEqual(cancelling["status"], "cancelling")
        self.assertEqual(cancelled["completed"], 0)

    def test_a_rebuild_stopped_for_memory_pressure_tells_the_person_why(self) -> None:
        self.server.close()
        self.thread.join(timeout=2)
        self.service.close()
        index = PressuredRelationshipIndex()
        self.service = HearthService(self.database, semantic_index=index)
        self.server = HearthWebServer(self.service, port=0, choose_file=lambda: self.selected_file, browser_opener=self.opened_urls.append)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._sign_in()
        self._json_request("POST", "api/import")
        index.block_rebuild = True
        preview = self._json_request("POST", "api/semantic-index/preview")["preview"]
        self._json_request("POST", f"api/previews/{preview['id']}/apply")

        failed = self._wait_for_semantic_job({"failed"})

        self.assertIn("memory pressure", failed["error"])

    def test_a_rebuild_without_memory_readings_says_it_is_unprotected(self) -> None:
        self.server.close()
        self.thread.join(timeout=2)
        self.service.close()
        index = UnmonitoredRelationshipIndex()
        self.service = HearthService(self.database, semantic_index=index)
        self.server = HearthWebServer(self.service, port=0, choose_file=lambda: self.selected_file, browser_opener=self.opened_urls.append)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._sign_in()
        self._json_request("POST", "api/import")
        preview = self._json_request("POST", "api/semantic-index/preview")["preview"]
        index.block_rebuild = True
        self._json_request("POST", f"api/previews/{preview['id']}/apply")

        completed = self._wait_for_semantic_job({"completed"})

        self.assertIn("not protected", completed["warning"])


    def test_collection_map_groups_documents_by_shared_section_without_source_content(self) -> None:
        self._json_request("POST", "api/import")
        second_note = self.root / "handoff.md"
        second_note.write_text("# Operations\n\nThe handoff owner is Lin.\n", encoding="utf-8")
        self.selected_file = second_note
        self._json_request("POST", "api/import")

        collection_map = self._json_request("GET", "api/map")

        self.assertEqual(len(collection_map["documents"]), 2)
        self.assertEqual(
            collection_map["clusters"],
            [{"id": "section:operations", "label": "Operations", "document_count": 2}],
        )
        self.assertEqual(
            collection_map["edges"],
            [
                {"document_id": 1, "cluster_id": "section:operations"},
                {"document_id": 2, "cluster_id": "section:operations"},
            ],
        )
        rendered = json.dumps(collection_map)
        self.assertNotIn(str(self.note), rendered)
        self.assertNotIn("The deployment owner is Ada.", rendered)

    def test_connected_folder_preview_requires_a_separate_apply_before_importing(self) -> None:
        source_root = self.root / "Desktop"
        source_root.mkdir()
        source_note = source_root / "brief.md"
        source_note.write_text("# Brief\n\nThe delivery is on Friday.\n", encoding="utf-8")
        self.server.close()
        self.thread.join(timeout=2)
        self.server = HearthWebServer(
            self.service,
            port=0,
            choose_file=lambda: self.selected_file,
            source_roots=(source_root,),
            browser_opener=self.opened_urls.append,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._sign_in()
        self._json_request("GET", "api/health")

        roots = self._json_request("GET", "api/sources")
        preview = self._json_request("POST", "api/sources/preview")["preview"]
        self.assertEqual(
            roots["roots"],
            [{"name": "Desktop", "status": "ready", "candidate_count": 0, "imported_count": 0}],
        )
        self.assertEqual(preview["candidate_count"], 1)
        self.assertEqual(preview["candidates"], [{"name": "brief.md", "source_root": "Desktop"}])
        self.assertEqual(self._json_request("GET", "api/documents")["documents"], [])
        self.assertNotIn(str(source_root), json.dumps(preview))

        applied = self._json_request("POST", f"api/source-previews/{preview['id']}/apply")["applied"]
        self.assertEqual(applied["action"], "import sources")
        self.assertEqual([item["document"]["name"] for item in applied["imported"]], ["brief.md"])
        self.assertEqual(self._json_request("GET", "api/documents")["documents"][0]["name"], "brief.md")

    def test_previewed_reindex_requires_one_apply_and_refreshes_search(self) -> None:
        self._json_request("POST", "api/import")
        self.note.write_text("# Operations\n\nThe deployment owner is Lin.\n", encoding="utf-8")
        preview = self._json_request("POST", "api/documents/1/actions/reindex/preview")["preview"]

        applied = self._json_request("POST", f"api/previews/{preview['id']}/apply")["applied"]
        reused_response, reused = self._request("POST", self._path(f"api/previews/{preview['id']}/apply"))
        answer = self._json_request("POST", "api/search/report", {"question": "Who is the deployment owner?"})

        self.assertEqual(applied["action"], "reindex")
        self.assertEqual(reused_response.status, 409)
        self.assertIn(b"Preview the action again", reused)
        self.assertIn("Lin", answer["evidence"][0]["excerpt"])

    def test_previewed_record_removal_leaves_the_source_file_untouched(self) -> None:
        self._json_request("POST", "api/import")
        preview = self._json_request("POST", "api/documents/1/actions/remove/preview")["preview"]
        applied = self._json_request("POST", f"api/previews/{preview['id']}/apply")["applied"]
        documents = self._json_request("GET", "api/documents")

        self.assertEqual(applied["action"], "remove")
        self.assertTrue(self.note.is_file())
        self.assertEqual(documents["documents"], [])

    def test_open_browser_uses_the_launch_link(self) -> None:
        self.assertIsNone(self.server.open_browser())
        self.assertEqual(self.opened_urls, [self.server.url])

    def _sign_in(self) -> None:
        """Redeem the server's launch link, as the browser does when hearth web opens it."""
        self._session: dict[str, str] = {}
        launched, _ = self._request("GET", urlsplit(self.server.url).path)
        self._session = {"Cookie": launched.getheader("Set-Cookie").split(";")[0],
                         "X-Hearth-Session": launched.getheader("Location").split("#session=")[1]}

    def _path(self, suffix: str = "") -> str:
        return f"/{suffix}"

    def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None, host: str | None = None
    ) -> tuple[http.client.HTTPResponse, bytes]:
        parsed = urlsplit(self.server.url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        encoded_payload = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {**self._session, "Origin": f"http://{parsed.hostname}:{parsed.port}"}
        headers |= {"Content-Type": "application/json"} if encoded_payload is not None else {}
        if host is None:
            connection.request(method, path, body=encoded_payload, headers=headers)
        else:
            connection.putrequest(method, path, skip_host=True)
            connection.putheader("Host", host)
            for name, value in headers.items():
                connection.putheader(name, value)
            if encoded_payload is not None:
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

    def _wait_for_semantic_job(self, statuses: set[str]) -> dict[str, object]:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            job = self._json_request("GET", "api/semantic-index")["job"]
            if job["status"] in statuses:
                return job
            time.sleep(0.01)
        self.fail("The local semantic-index job did not reach the expected state.")


if __name__ == "__main__":
    unittest.main()


class WorkbenchTasksEndpointTests(unittest.TestCase):
    """The dashboard's task data comes from a function the CLI passes in, behind the capability path."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.service = HearthService(Path(self.temporary_directory.name) / "hearth.sqlite")
        self.addCleanup(self.temporary_directory.cleanup)
        self.addCleanup(self.service.close)

    def test_the_endpoint_serves_the_passed_in_task_data(self) -> None:
        payload = {"tasks": [{"id": "20260930-000000", "status": "running"}], "slots": {"implement": {"limit": 2, "busy": 1}}}
        app = HearthWebApplication(self.service, "token", workbench_tasks=lambda: payload)

        response = signed_in(app)("GET", "/api/workbench/tasks")
        outside = app.respond("GET", "/api/workbench/tasks", b"", {"Host": HOST})

        self.assertEqual((response.status, json.loads(response.body)), (200, payload))
        self.assertEqual(outside.status, 401)

    def test_without_the_workbench_the_endpoint_says_so(self) -> None:
        response = signed_in(HearthWebApplication(self.service, "token"))("GET", "/api/workbench/tasks")

        self.assertEqual(response.status, 404)
        self.assertIn("not connected", json.loads(response.body)["error"])

    def test_the_transcript_route_passes_task_run_and_offset_and_rejects_other_shapes(self) -> None:
        calls = []
        app = HearthWebApplication(self.service, "token", workbench_transcript=lambda *args: calls.append(args) or {"items": []})

        ask = signed_in(app)
        ok = ask("GET", "/api/workbench/transcript/20260930-120000/01-implement-claude/42")
        bad = ask("GET", "/api/workbench/transcript/20260930-120000/01-implement-claude/x")
        unknown = signed_in(HearthWebApplication(self.service, "token", workbench_transcript=lambda *args: None))(
            "GET", "/api/workbench/transcript/20260930-120000/01-implement-claude/0")

        self.assertEqual((ok.status, calls), (200, [("20260930-120000", "01-implement-claude", 42)]))
        self.assertEqual((bad.status, unknown.status), (400, 404))


    def test_the_diff_route_passes_the_task_and_rejects_other_shapes(self) -> None:
        calls = []
        app = HearthWebApplication(self.service, "token", workbench_diff=lambda task: calls.append(task) or {"diff": None})

        ask = signed_in(app)
        ok = ask("GET", "/api/workbench/diff/20260930-120000")
        bad = ask("GET", "/api/workbench/diff/20260930-120000/extra")
        unknown = signed_in(HearthWebApplication(self.service, "token", workbench_diff=lambda task: None))(
            "GET", "/api/workbench/diff/20260930-120000")

        self.assertEqual((ok.status, calls), (200, ["20260930-120000"]))
        self.assertEqual((bad.status, unknown.status), (400, 404))


class SlowCleanupIndex(BlockingRelationshipIndex):
    """Takes a while to clean up after a cancellation, as a real build does when it removes its staging folder."""

    def __init__(self) -> None:
        super().__init__()
        self.cleaned = threading.Event()

    def rebuild(self, chunks, *, on_progress=None, is_cancelled=None, on_warning=None) -> None:
        try:
            super().rebuild(chunks, on_progress=on_progress, is_cancelled=is_cancelled, on_warning=on_warning)
        finally:
            time.sleep(0.5)
            self.cleaned.set()


class RebuildLifecycleTests(unittest.TestCase):
    """ADR-0038: a rebuild's state survives a crash, and stopping the backend waits for a rebuild to finish cleaning up."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.state = self.root / ".hearth/rebuild.json"
        note = self.root / "note.md"
        note.write_text("# Deployment\n\nThe platform team owns deployment.\n", encoding="utf-8")
        self.index = SlowCleanupIndex()
        self.service = HearthService(self.root / "hearth.sqlite", semantic_index=self.index)
        self.addCleanup(self.service.close)
        self.service.import_with_summary(str(note))

    def app(self) -> tuple[HearthWebApplication, object]:
        app = HearthWebApplication(self.service, "token", rebuild_state=self.state)
        return app, signed_in(app)

    def start_rebuild(self, call) -> None:
        self.index.block_rebuild = True
        self.index.cleaned.clear()  # The import in setUp already ran one rebuild through this index.
        preview = json.loads(call("POST", "/api/semantic-index/preview").body)["preview"]
        call("POST", f"/api/previews/{preview['id']}/apply")
        self.assertTrue(self.index.started.wait(timeout=5))

    def test_a_rebuild_records_its_state_on_disk_from_start_to_end(self) -> None:
        app, call = self.app()
        self.start_rebuild(call)

        running = json.loads(self.state.read_text(encoding="utf-8"))
        app.stop_semantic_index_rebuild()

        self.assertEqual(running["status"], "running")
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["status"], "cancelled")

    def record(self, status: str = "running") -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps({"status": status, "at": "2026-10-08T20:00:00-0400"}), encoding="utf-8")

    def claimed_elsewhere(self) -> None:
        """Hold the rebuild claim as another live backend would."""
        handle = (self.state.parent / "rebuild.lock").open("a")
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_a_rebuild_whose_backend_died_is_reported_as_interrupted_on_the_next_launch(self) -> None:
        self.record()  # Its owner is gone, so nothing holds the claim.
        _, call = self.app()

        job = json.loads(call("GET", "/api/semantic-index").body)["job"]

        self.assertEqual(job["status"], "interrupted")

    def test_a_rebuild_whose_backend_is_alive_is_not_reported_as_interrupted(self) -> None:
        self.record()
        self.claimed_elsewhere()
        _, call = self.app()

        self.assertEqual(json.loads(call("GET", "/api/semantic-index").body)["job"]["status"], "idle")

    def test_a_rebuild_whose_state_cannot_be_written_does_not_start_or_block_the_next(self) -> None:
        app, call = self.app()
        preview = json.loads(call("POST", "/api/semantic-index/preview").body)["preview"]

        with mock.patch.object(web, "_write_rebuild_state", side_effect=OSError("disk full")):
            self.assertEqual(call("POST", f"/api/previews/{preview['id']}/apply").status, 400)

        self.assertEqual(json.loads(call("GET", "/api/semantic-index").body)["job"]["status"], "idle")
        self.start_rebuild(call)  # Neither refused as "already running" nor by a claim the failed start kept.
        app.stop_semantic_index_rebuild()

    def test_a_second_backend_cannot_start_a_rebuild_while_another_owns_the_record(self) -> None:
        first, first_call = self.app()
        self.start_rebuild(first_call)
        second, second_call = self.app()  # Another backend on the same ~/.hearth, as the desktop app beside hearth web.
        preview = json.loads(second_call("POST", "/api/semantic-index/preview").body)["preview"]

        refused = second_call("POST", f"/api/previews/{preview['id']}/apply")
        recorded = json.loads(self.state.read_text(encoding="utf-8"))["status"]
        first.stop_semantic_index_rebuild()

        self.assertEqual((refused.status, recorded), (400, "running"))
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["status"], "cancelled")

    def test_a_finished_rebuild_publishes_its_state_before_the_next_can_start(self) -> None:
        app, call = self.app()
        first = json.loads(call("POST", "/api/semantic-index/preview").body)["preview"]
        second = json.loads(call("POST", "/api/semantic-index/preview").body)["preview"]
        writing_final, release = threading.Event(), threading.Event()
        write = web._write_rebuild_state

        def held_final(path: Path, status: str) -> None:
            if status == "completed":
                writing_final.set()
                release.wait(timeout=10)
            write(path, status)

        with mock.patch.object(web, "_write_rebuild_state", side_effect=held_final):
            call("POST", f"/api/previews/{first['id']}/apply")  # Completes at once; its final write is held here.
            self.assertTrue(writing_final.wait(timeout=5))
            self.index.block_rebuild = True
            applied = {}
            starting = threading.Thread(target=lambda: applied.update(response=call("POST", f"/api/previews/{second['id']}/apply")))
            starting.start()
            time.sleep(0.3)
            self.assertTrue(starting.is_alive())  # Waiting for the first rebuild's state, not refused.
            release.set()
            starting.join(timeout=5)
        self.assertTrue(self.index.started.wait(timeout=5))
        recorded = json.loads(self.state.read_text(encoding="utf-8"))["status"]
        app.stop_semantic_index_rebuild()

        self.assertEqual((applied["response"].status, recorded), (200, "running"))  # The second rebuild's, not a late "completed".

    def test_a_rebuild_that_fails_releases_its_claim(self) -> None:
        app, call = self.app()
        preview = json.loads(call("POST", "/api/semantic-index/preview").body)["preview"]

        with mock.patch.object(self.index, "rebuild", side_effect=IndexBusy("Another Hearth semantic-index build is running.")):
            call("POST", f"/api/previews/{preview['id']}/apply")  # As when a hearth import's own rebuild holds the build lock.
            for _ in range(100):
                if json.loads(call("GET", "/api/semantic-index").body)["job"]["status"] == "failed":
                    break
                time.sleep(0.05)

        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["status"], "failed")
        self.start_rebuild(call)  # Not refused by a claim the failed rebuild kept.
        app.stop_semantic_index_rebuild()

    def test_a_rebuild_whose_thread_cannot_start_releases_its_claim(self) -> None:
        app, call = self.app()
        preview = json.loads(call("POST", "/api/semantic-index/preview").body)["preview"]

        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")):
            self.assertEqual(call("POST", f"/api/previews/{preview['id']}/apply").status, 500)

        self.assertEqual(json.loads(call("GET", "/api/semantic-index").body)["job"]["status"], "idle")
        self.start_rebuild(call)  # Neither refused as "already running" nor by a claim the failed start kept.
        app.stop_semantic_index_rebuild()

    def test_a_start_interrupted_before_its_thread_runs_is_cleaned_up_by_stopping(self) -> None:
        app, call = self.app()
        preview = json.loads(call("POST", "/api/semantic-index/preview").body)["preview"]

        with mock.patch.object(threading.Thread, "start", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            call("POST", f"/api/previews/{preview['id']}/apply")
        app.stop_semantic_index_rebuild()  # As shutdown does after Ctrl+C.

        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["status"], "cancelled")
        other_app, other = self.app()  # The claim is free for another backend.
        self.addCleanup(other_app.stop_semantic_index_rebuild)
        self.start_rebuild(other)

    def test_an_interrupt_after_the_thread_started_keeps_the_rebuild_owned_until_it_stops(self) -> None:
        app, call = self.app()
        self.index.block_rebuild = True
        self.index.cleaned.clear()  # The import in setUp already ran one rebuild through this index.
        preview = json.loads(call("POST", "/api/semantic-index/preview").body)["preview"]
        start = threading.Thread.start

        def started_then_interrupted(thread) -> None:
            start(thread)
            raise KeyboardInterrupt  # Ctrl+C lands while start() waits for the thread to report in.

        with mock.patch.object(threading.Thread, "start", started_then_interrupted), self.assertRaises(KeyboardInterrupt):
            call("POST", f"/api/previews/{preview['id']}/apply")
        _, other = self.app()
        other_preview = json.loads(other("POST", "/api/semantic-index/preview").body)["preview"]
        refused = other("POST", f"/api/previews/{other_preview['id']}/apply").status
        app.stop_semantic_index_rebuild()

        self.assertEqual(refused, 400)  # The running rebuild still owns the record.
        self.assertTrue(self.index.cleaned.is_set())  # Stopping waited for the rebuild the interrupt left running.
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["status"], "cancelled")

    def test_a_rebuild_that_finishes_while_its_record_is_read_is_not_reported_as_interrupted(self) -> None:
        self.record()
        owner = (self.state.parent / "rebuild.lock").open("a")
        self.addCleanup(owner.close)
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, call = self.app()
        flock = fcntl.flock

        def owner_finishes_first(handle, operation) -> None:
            if operation & fcntl.LOCK_SH:  # The other backend finishes just as this one checks its claim.
                web._write_rebuild_state(self.state, "cancelled")
                owner.close()
            flock(handle, operation)

        with mock.patch.object(web.fcntl, "flock", side_effect=owner_finishes_first):
            job = json.loads(call("GET", "/api/semantic-index").body)["job"]

        self.assertEqual(job["status"], "idle")

    def test_no_rebuild_starts_once_stopping_has_begun(self) -> None:
        app, call = self.app()
        app.stop_semantic_index_rebuild()
        preview = json.loads(call("POST", "/api/semantic-index/preview").body)["preview"]

        response = call("POST", f"/api/previews/{preview['id']}/apply")

        self.assertEqual(response.status, 400)
        self.assertFalse(self.state.exists())

    def test_stopping_waits_until_the_rebuild_has_cleaned_up(self) -> None:
        app, call = self.app()
        self.start_rebuild(call)

        app.stop_semantic_index_rebuild()

        self.assertTrue(self.index.cleaned.is_set())
        self.assertEqual(json.loads(call("GET", "/api/semantic-index").body)["job"]["status"], "cancelled")

    def test_the_server_waits_for_the_rebuild_when_it_closes(self) -> None:
        server = HearthWebServer(self.service, port=0, rebuild_state=self.state)
        self.start_rebuild(signed_in(server._application))

        server.close()

        self.assertTrue(self.index.cleaned.is_set())

    def test_hearth_web_reports_an_interrupted_rebuild_and_stops_through_its_normal_shutdown_on_sigterm(self) -> None:
        self.state = self.root / "elsewhere/rebuild.json"  # Under HEARTH_HOME, which the started process takes from its environment.
        self.record()  # Left by a backend that crashed.
        env = {"HOME": str(self.root), "HEARTH_HOME": str(self.root / "elsewhere"), "PATH": "/usr/bin:/bin",
               "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen([sys.executable, "-m", "hearth.cli", "--database", str(self.root / "web.sqlite"), "web", "--port", str(port),
                                    "--no-open"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        launch = urlsplit(process.stdout.readline().split(" at ")[1].strip())
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", launch.path)
        redeemed = connection.getresponse()
        redeemed.read()
        session = {"Cookie": redeemed.getheader("Set-Cookie").split(";")[0],
                   "X-Hearth-Session": redeemed.getheader("Location").split("#session=")[1]}
        connection.request("GET", "/api/semantic-index", headers=session)
        self.assertEqual(json.loads(connection.getresponse().read())["job"]["status"], "interrupted")
        connection.close()

        process.send_signal(signal.SIGTERM)

        self.assertEqual(process.wait(timeout=10), 0)
        self.assertIn("stopped", process.stdout.read())
        process.stderr.close()
        process.stdout.close()


class DesktopModeTests(unittest.TestCase):
    """ADR-0038: the desktop backend binds port 0, hands its secrets only to the shell's pipe, and proves it holds them."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def backend(self) -> tuple[subprocess.Popen, dict]:
        read, write = os.pipe()
        process = subprocess.Popen([sys.executable, "-m", "hearth.cli", "--database", str(self.root / "web.sqlite"), "web", "--desktop",
                                    "--handshake-fd", str(write)], env=self.env(), pass_fds=(write,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        os.close(write)
        self.addCleanup(lambda: (process.kill(), process.wait(), process.stdout.close(), process.stderr.close()))
        with os.fdopen(read, encoding="utf-8") as pipe:
            handshake = json.loads(pipe.readline())
        return process, handshake

    def request(self, handshake: dict, path: str, credentials: bool = True) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", handshake["port"], timeout=5)
        headers = {"Cookie": f"{handshake['cookie_name']}={handshake['cookie']}", "X-Hearth-Session": handshake["token"]} if credentials else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    @staticmethod
    def answer(token: str, challenge: str) -> str:
        return hmac.new(token.encode(), b"hearth-desktop-challenge\0" + challenge.encode(), hashlib.sha256).hexdigest()

    def test_the_backend_proves_it_holds_this_launchs_secret(self) -> None:
        _, handshake = self.backend()
        challenge = secrets.token_hex(32)

        status, body = self.request(handshake, f"/desktop/challenge?c={challenge}", credentials=False)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["answer"], self.answer(handshake["token"], challenge))
        self.assertNotEqual(json.loads(body)["answer"], self.answer("another launch's token", challenge))
        for bad in ("short", "z" * 64, "a" * 300, ""):
            with self.subTest(challenge=bad):
                self.assertEqual(self.request(handshake, f"/desktop/challenge?c={bad}", credentials=False)[0], 400)

    def test_api_routes_need_the_handed_over_credentials_and_there_is_no_launch_link(self) -> None:
        _, handshake = self.backend()

        self.assertEqual(self.request(handshake, "/api/workbench/tasks")[0], 200)
        self.assertEqual(self.request(handshake, "/api/workbench/tasks", credentials=False)[0], 401)
        self.assertEqual(self.request(handshake, "/launch/anything", credentials=False)[0], 404)

    def test_the_secrets_never_reach_the_backends_output(self) -> None:
        process, handshake = self.backend()
        self.request(handshake, "/api/workbench/tasks")

        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 0)
        output = process.stdout.read() + process.stderr.read()

        for secret in (handshake["cookie"], handshake["token"]):
            self.assertNotIn(secret, output)

    def test_a_restarted_backend_does_not_accept_the_old_secrets(self) -> None:
        first, old = self.backend()
        first.send_signal(signal.SIGTERM)
        first.wait(timeout=10)
        _, new = self.backend()
        stale = {**new, "cookie": old["cookie"], "token": old["token"]}
        challenge = secrets.token_hex(32)

        self.assertEqual(self.request(stale, "/api/workbench/tasks")[0], 401)
        answer = json.loads(self.request(new, f"/desktop/challenge?c={challenge}", credentials=False)[1])["answer"]
        self.assertNotEqual(answer, self.answer(old["token"], challenge))

    def test_only_the_desktop_backend_answers_a_challenge(self) -> None:
        service = HearthService(self.root / "browser.sqlite")
        self.addCleanup(service.close)
        app = HearthWebApplication(service, "token")
        call = signed_in(app)  # A browser session exists, so only the mode can refuse.

        response = call("GET", f"/desktop/challenge?c={secrets.token_hex(32)}")

        self.assertNotEqual(response.status, 200)
        self.assertNotIn(b"answer", response.body)

    def test_the_desktop_backend_has_no_launch_link_even_for_its_own_token(self) -> None:
        service = HearthService(self.root / "desktop.sqlite")
        self.addCleanup(service.close)
        app = HearthWebApplication(service, "token")
        app.start_desktop_session(8765)

        self.assertEqual(app.respond("GET", "/launch/token", b"", {"Host": HOST}).status, 404)

    def test_two_desktop_backends_each_bind_a_free_port(self) -> None:
        _, first = self.backend()
        _, second = self.backend()

        self.assertNotEqual(first["port"], second["port"])
        self.assertEqual((self.request(first, "/api/workbench/tasks")[0], self.request(second, "/api/workbench/tasks")[0]), (200, 200))

    def env(self, **extra: str) -> dict[str, str]:
        return {"HOME": str(self.root), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).parents[1] / "src"), **extra}

    def test_desktop_flags_that_would_be_ignored_are_refused(self) -> None:
        for flags, named in ((["--desktop"], "--handshake-fd"), (["--handshake-fd", "3"], "--desktop"),
                             (["--desktop", "--handshake-fd", "3", "--port", "9000"], "--port")):
            with self.subTest(flags=flags):
                result = subprocess.run([sys.executable, "-m", "hearth.cli", "--database", str(self.root / "web.sqlite"), "web", *flags],
                                        env=self.env(), capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 2)
                self.assertIn(named, result.stderr)

    def test_the_handshake_goes_only_to_a_writable_pipe(self) -> None:
        command = [sys.executable, "-m", "hearth.cli", "--database", str(self.root / "web.sqlite"), "web", "--desktop", "--handshake-fd"]
        stdout = subprocess.run([*command, "1"], env=self.env(), capture_output=True, text=True, timeout=30)
        saved = self.root / "handshake.txt"
        with saved.open("w") as file:
            to_file = subprocess.run([*command, str(file.fileno())], env=self.env(), pass_fds=(file.fileno(),), capture_output=True, text=True,
                                     timeout=30)
        read, write = os.pipe()
        read_end = subprocess.run([*command, str(read)], env=self.env(), pass_fds=(read,), capture_output=True, text=True, timeout=30)
        os.close(read)
        os.close(write)

        for name, result in (("stdout", stdout), ("a file", to_file), ("a pipe's read end", read_end)):
            with self.subTest(destination=name):
                self.assertEqual(result.returncode, 2)
                self.assertIn("writable pipe", result.stderr)
                self.assertNotIn("token", result.stdout)
        self.assertEqual(saved.read_text(encoding="utf-8"), "")

    def test_the_handshake_may_go_to_an_unnamed_socket_pair_but_not_a_named_socket(self) -> None:
        # Electron's Node gives a child an unnamed socket pair for an extra stdio pipe, which a FIFO-only check refused (2026-10-09).
        command = [sys.executable, "-m", "hearth.cli", "--database", str(self.root / "web.sqlite"), "web", "--desktop", "--handshake-fd"]
        ours, theirs = socket.socketpair()
        process = subprocess.Popen([*command, str(theirs.fileno())], env=self.env(), pass_fds=(theirs.fileno(),),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        theirs.close()
        self.addCleanup(lambda: (process.kill(), process.wait(), ours.close()))
        with ours.makefile(encoding="utf-8") as pipe:
            self.assertIn("token", json.loads(pipe.readline()))
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.root / "s"))
        self.addCleanup(listener.close)

        named = subprocess.run([*command, str(listener.fileno())], env=self.env(), pass_fds=(listener.fileno(),), capture_output=True,
                               text=True, timeout=30)

        self.assertEqual(named.returncode, 2)
        self.assertIn("writable pipe", named.stderr)

    def test_a_backend_whose_app_died_during_startup_stops(self) -> None:
        read, write = os.pipe()  # This test keeps the read end, so the handshake still has somewhere to go.
        launcher = subprocess.run([sys.executable, "-c", """
import subprocess, sys
backend = subprocess.Popen([sys.executable, "-m", "hearth.cli", "--database", sys.argv[1], "web", "--desktop", "--handshake-fd", sys.argv[2]],
                           pass_fds=(int(sys.argv[2]),), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(backend.pid)  # Then the app exits at once, before the backend has finished starting.
""", str(self.root / "web.sqlite"), str(write)], env=self.env(), pass_fds=(write,), capture_output=True, text=True, timeout=30)
        os.close(write)
        backend = int(launcher.stdout)

        def kill_backend() -> None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(backend, signal.SIGKILL)

        self.addCleanup(kill_backend)
        with os.fdopen(read, encoding="utf-8") as pipe:
            pipe.readline()  # A handshake, or nothing if it refused to start as an orphan.
        for _ in range(100):
            try:
                os.kill(backend, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail("a backend orphaned during startup kept running")

    def test_a_backend_whose_app_dies_after_it_started_up_but_before_the_handshake_stops(self) -> None:
        database = self.root / "web.sqlite"
        read, write = os.pipe()
        launcher = subprocess.Popen([sys.executable, "-c", """
import subprocess, sys, time
backend = subprocess.Popen([sys.executable, "-m", "hearth.cli", "--database", sys.argv[1], "web", "--desktop", "--handshake-fd", sys.argv[2]],
                           pass_fds=(int(sys.argv[2]),), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(backend.pid, flush=True)
time.sleep(60)
""", str(database), str(write)], env=self.env(), pass_fds=(write,), stdout=subprocess.PIPE, text=True)
        os.close(write)
        self.addCleanup(launcher.stdout.close)
        backend = int(launcher.stdout.readline())

        def kill_backend() -> None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(backend, signal.SIGCONT)
                os.kill(backend, signal.SIGKILL)

        self.addCleanup(kill_backend)
        while not database.exists():  # It has recorded its launcher and opened the database; the handshake comes later.
            time.sleep(0.001)
        os.kill(backend, signal.SIGSTOP)
        launcher.kill()  # The app crashes in that window.
        launcher.wait()
        os.kill(backend, signal.SIGCONT)
        with os.fdopen(read, encoding="utf-8") as pipe:
            pipe.readline()
        for _ in range(100):
            try:
                os.kill(backend, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail("the backend adopted its new parent as its owner and kept running")

    def test_an_agent_cannot_start_a_dashboard_backend(self) -> None:
        for flags in ([], ["--desktop"]):
            with self.subTest(flags=flags):
                read, write = os.pipe()
                result = subprocess.run([sys.executable, "-m", "hearth.cli", "--database", str(self.root / "web.sqlite"), "web", "--no-open",
                                         *(flags + ["--handshake-fd", str(write)] if flags else [])], env=self.env(HEARTH_TASK="20260927-000000"),
                                        pass_fds=(write,), capture_output=True, text=True, timeout=30)
                os.close(write)
                with os.fdopen(read, encoding="utf-8") as pipe:
                    self.assertEqual(pipe.read(), "")
                self.assertEqual(result.returncode, 1)
                self.assertIn("Agents cannot", result.stderr)

    def test_the_desktop_backend_stops_when_the_app_that_started_it_dies(self) -> None:
        launcher = subprocess.Popen([sys.executable, "-c", """
import json, os, subprocess, sys, time
read, write = os.pipe()
backend = subprocess.Popen([sys.executable, "-m", "hearth.cli", "--database", sys.argv[1], "web", "--desktop", "--handshake-fd", str(write)],
                           pass_fds=(write,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.close(write)
print(backend.pid, json.loads(os.fdopen(read).readline())["port"], flush=True)
time.sleep(60)
""", str(self.root / "web.sqlite")], env=self.env(), stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(launcher.stdout.close)
        backend, port = map(int, launcher.stdout.readline().split())

        def kill_backend() -> None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(backend, signal.SIGKILL)  # Only if the test failed before the backend stopped by itself.

        self.addCleanup(kill_backend)

        launcher.kill()  # The app crashes; the backend is left without its parent.
        launcher.wait()
        for _ in range(100):
            try:
                os.kill(backend, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the orphaned backend kept running")
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()


class SessionTests(unittest.TestCase):
    """ADR-0037: a launch link that works once, a cookie for the page, and cookie plus header token for every API route."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.service = HearthService(Path(self.temporary_directory.name) / "hearth.sqlite")
        self.addCleanup(self.temporary_directory.cleanup)
        self.addCleanup(self.service.close)
        self.app = HearthWebApplication(self.service, "launch-secret", workbench_tasks=lambda: {"tasks": []})

    def launch(self):
        return self.app.respond("GET", self.app.launch_path, b"", {"Host": HOST})

    def credentials(self) -> tuple[str, str]:
        launched = dict(self.launch().headers)
        return launched["Set-Cookie"].split(";")[0], launched["Location"].split("#session=")[1]

    def test_the_launch_link_works_once_and_sets_a_strict_http_only_cookie(self) -> None:
        first, second = self.launch(), self.launch()
        wrong = self.app.respond("GET", "/launch/guess", b"", {"Host": HOST})

        headers = dict(first.headers)
        self.assertEqual(first.status, 303)
        self.assertTrue(headers["Location"].startswith("/#session="))
        self.assertRegex(headers["Set-Cookie"], r"^hearth_8765=[^;]+; HttpOnly; SameSite=Strict; Path=/$")
        self.assertEqual((second.status, wrong.status), (410, 404))
        self.assertNotIn("Set-Cookie", dict(second.headers))
        self.assertEqual(self.app.respond("GET", "/launch-secret/", b"", {"Host": HOST}).status, 401)  # The old capability path.

    def test_only_one_of_simultaneous_redemptions_wins(self) -> None:
        results: list[int] = []
        barrier = threading.Barrier(8)

        def redeem() -> None:
            barrier.wait()
            results.append(self.launch().status)

        threads = [threading.Thread(target=redeem) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results), [303] + [410] * 7)

    def test_the_page_and_its_files_need_the_cookie(self) -> None:
        cookie, _ = self.credentials()
        for path in ("/", "/assets/dashboard.js", "/assets/knowledge.js"):
            with self.subTest(path=path):
                refused = self.app.respond("GET", path, b"", {"Host": HOST})
                allowed = self.app.respond("GET", path, b"", {"Host": HOST, "Cookie": cookie})
                self.assertEqual((refused.status, allowed.status), (401, 200))
        self.assertIn(b"hearth web", self.app.respond("GET", "/", b"", {"Host": HOST}).body)

    def test_api_routes_need_both_the_cookie_and_the_header_token(self) -> None:
        cookie, token = self.credentials()
        other = HearthWebApplication(self.service, "other-secret")
        other_cookie = dict(other.respond("GET", other.launch_path, b"", {"Host": HOST}).headers)["Set-Cookie"].split(";")[0]
        cases = {
            "neither": {},
            "cookie only": {"Cookie": cookie},
            "token only": {"X-Hearth-Session": token},
            "wrong token": {"Cookie": cookie, "X-Hearth-Session": token[:-1] + ("x" if token[-1] != "x" else "y")},
            "another server's cookie": {"Cookie": other_cookie, "X-Hearth-Session": token},
        }
        for name, headers in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.app.respond("GET", "/api/workbench/tasks", b"", {"Host": HOST, **headers}).status, 401)
        both = self.app.respond("GET", "/api/workbench/tasks", b"", {"Host": HOST, "Cookie": f"theme=dark; {cookie}", "X-Hearth-Session": token})
        self.assertEqual(both.status, 200)

    def test_a_post_needs_the_servers_own_origin(self) -> None:
        cookie, token = self.credentials()
        session = {"Host": HOST, "Cookie": cookie, "X-Hearth-Session": token}
        body = json.dumps({"question": "deployment owner"}).encode()
        for origin in (None, "null", "http://127.0.0.1:3000", "http://evil.test"):
            with self.subTest(origin=origin):
                headers = session | ({"Origin": origin} if origin is not None else {})
                self.assertEqual(self.app.respond("POST", "/api/search/report", body, headers).status, 403)
        self.assertEqual(self.app.respond("POST", "/api/search/report", body, session | {"Origin": f"http://{HOST}"}).status, 200)


class TaskPreviewTests(unittest.TestCase):
    """ADR-0037 previews: single-use, two minutes, bound to session, task, action, input, and the state they were shown against."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.service = HearthService(Path(self.temporary_directory.name) / "hearth.sqlite")
        self.addCleanup(self.temporary_directory.cleanup)
        self.addCleanup(self.service.close)
        self.state = {"t1": "waiting:1:m3", "t2": "waiting:1:m3"}  # Equal, so only the task binding can refuse t2.
        self.applied: list[tuple[str, str, dict]] = []
        self.app = HearthWebApplication(
            self.service, "token",
            workbench_task_state=lambda task_id, action: self.state.get(task_id),
            workbench_task_action=lambda task_id, action, arguments, fingerprint: self.applied.append((task_id, action, dict(arguments))) or {"done": True},
        )
        self.call = signed_in(self.app)

    def preview(self, task_id: str = "t1", action: str = "answer", arguments: dict | None = None) -> str:
        body = json.dumps({"arguments": arguments if arguments is not None else {"text": "Use the staging bucket."}}).encode()
        response = self.call("POST", f"/api/tasks/{task_id}/actions/{action}/preview", body)
        self.assertEqual(response.status, 200, response.body)
        return json.loads(response.body)["preview"]["id"]

    def apply(self, preview_id: str, task_id: str = "t1", action: str = "answer", call=None):
        return (call or self.call)("POST", f"/api/tasks/{task_id}/actions/{action}/apply", json.dumps({"preview": preview_id}).encode())

    def test_an_applied_preview_runs_the_previewed_action_once(self) -> None:
        preview_id = self.preview()

        self.assertEqual(self.applied, [])
        self.assertEqual(self.apply(preview_id).status, 200)
        self.assertEqual(self.applied, [("t1", "answer", {"text": "Use the staging bucket."})])
        self.assertEqual(self.apply(preview_id).status, 409)
        self.assertEqual(len(self.applied), 1)

    def test_an_expired_preview_is_refused(self) -> None:
        with mock.patch("hearth.web.time.monotonic", return_value=1000.0):
            preview_id = self.preview()
        with mock.patch("hearth.web.time.monotonic", return_value=1000.0 + 121):
            self.assertEqual(self.apply(preview_id).status, 409)
        self.assertEqual(self.applied, [])

    def test_a_preview_applied_to_another_task_or_action_is_refused_and_used_up(self) -> None:
        for task_id, action in (("t2", "answer"), ("t1", "cancel")):
            with self.subTest(task=task_id, action=action):
                preview_id = self.preview()
                self.assertEqual(self.apply(preview_id, task_id, action).status, 409)
                self.assertEqual(self.apply(preview_id).status, 409)
        self.assertEqual(self.applied, [])

    def test_a_preview_from_another_session_is_refused(self) -> None:
        preview_id = self.preview()
        # ADR-0038 may issue a new session to the app; simulate one so the preview's session no longer matches.
        self.app._session = None
        self.app._launch_token = "second"
        other = signed_in(self.app)

        self.assertEqual(self.apply(preview_id, call=other).status, 409)
        self.assertEqual(self.applied, [])

    def test_a_change_in_the_task_after_the_preview_is_refused(self) -> None:
        preview_id = self.preview()
        self.state["t1"] = "waiting:2:m4"

        self.assertEqual(self.apply(preview_id).status, 409)
        self.assertEqual(self.applied, [])

    def test_previews_are_refused_for_a_get_an_unknown_action_a_missing_task_or_bad_input(self) -> None:
        preview_id = self.preview()
        self.assertNotEqual(self.call("GET", f"/api/tasks/t1/actions/answer/apply?preview={preview_id}").status, 200)
        for path, body in (("/api/tasks/t1/actions/publish/preview", b"{}"),
                           ("/api/tasks/nope/actions/answer/preview", b"{}"),
                           ("/api/tasks/t1/actions/answer/preview", json.dumps({"arguments": {"text": 3}}).encode())):
            with self.subTest(path=path, body=body):
                self.assertNotEqual(self.call("POST", path, body).status, 200)
        self.assertEqual(self.applied, [])

    def test_an_empty_answer_is_refused_before_any_preview(self) -> None:
        for text in ("", "   "):
            with self.subTest(text=text):
                response = self.call("POST", "/api/tasks/t1/actions/answer/preview", json.dumps({"arguments": {"text": text}}).encode())
                self.assertEqual(response.status, 400)
        self.assertEqual(self.app._pending_task_actions, {})

    def test_an_apply_from_a_foreign_origin_is_refused(self) -> None:
        preview_id = self.preview()
        foreign = {**self.call.headers, "Origin": "http://127.0.0.1:3000"}
        self.assertEqual(self.apply(preview_id, call=lambda method, path, body: self.app.respond(method, path, body, foreign)).status, 403)
        self.assertEqual(self.applied, [])

    def test_task_actions_do_not_exist_until_the_cli_passes_them(self) -> None:
        call = signed_in(HearthWebApplication(self.service, "plain"))

        self.assertEqual(call("POST", "/api/tasks/t1/actions/cancel/preview", b"{}").status, 404)


class DashboardPageTests(unittest.TestCase):
    """The dashboard is the home page and holds the knowledge tools; nothing loads from another host (UI-4)."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.service = HearthService(Path(self.temporary_directory.name) / "hearth.sqlite")
        self.addCleanup(self.temporary_directory.cleanup)
        self.addCleanup(self.service.close)
        self.app = HearthWebApplication(self.service, "token")
        self.call = signed_in(self.app)

    def test_the_home_page_is_the_dashboard_and_the_classic_knowledge_page_is_gone(self) -> None:
        home = self.call("GET", "/")

        self.assertIn(b"Needs you", home.body)
        for path in ("/knowledge", "/assets/app.js", "/assets/app.css"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET", path).status, 404)
        for name in ("index.html", "app.js", "app.css"):
            self.assertFalse((ASSETS / name).exists(), name)

    def test_the_knowledge_tab_has_import_folders_map_and_index_tools(self) -> None:
        home = self.call("GET", "/").body.decode()
        script = self.call("GET", "/assets/knowledge.js")

        for element in ("kn-import", "kn-scan", "kn-map", "kn-index", "kn-warning", "kn-panel"):
            self.assertIn(f'id="{element}"', home)
        self.assertIn('src="assets/knowledge.js"', home)
        self.assertEqual((script.status, script.content_type.split(";")[0]), (200, "application/javascript"))
        for route in ("import", "sources/preview", "source-previews/", "semantic-index/preview", "semantic-index/cancel", "/actions/", "previews/",
                      "elapsed_seconds", "cpu_seconds", "peak_resident_memory_bytes", "evidence_units_per_minute", "job.warning",
                      '"interrupted"'):
            self.assertIn(route, script.body.decode())

    def test_the_removed_answer_route_is_gone(self) -> None:
        self.assertEqual(self.call("POST", "/api/search", b'{"question": "x"}').status, 404)

    def test_the_shell_has_hub_tasks_and_knowledge_views_and_a_default_view_setting(self) -> None:
        home = self.call("GET", "/").body.decode()

        for view in ("hub", "tasks", "knowledge"):
            with self.subTest(view=view):
                self.assertIn(f'href="#/{view}" data-view="{view}"', home)
                self.assertRegex(home, f'class="view[^"]*" data-view="{view}"')
        self.assertIn('id="default-view"', home)
        self.assertIn('id="hub-needs"', home)

    def test_dashboard_assets_load_nothing_from_another_host_and_never_insert_html(self) -> None:
        for name, kind in (("assets/dashboard.css", "text/css"), ("assets/dashboard.js", "application/javascript"),
                           ("assets/hub.js", "application/javascript"), ("assets/knowledge.js", "application/javascript")):
            with self.subTest(asset=name):
                response = self.call("GET", f"/{name}")
                self.assertEqual((response.status, response.content_type.split(";")[0]), (200, kind))
                # The SVG namespace is an identifier, not a request; nothing else may name another host.
                body = response.body.decode().replace('"http://www.w3.org/2000/svg"', "")
                self.assertNotRegex(body, r"https?://|@import|url\(")
                self.assertNotIn("innerHTML", body)
                self.assertNotIn("insertAdjacentHTML", body)


ASSETS = Path(__file__).parents[1] / "src/hearth/web_assets"
HOSTILE_URLS = ["javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                "http://example.test/pr/1", "//example.test/pr/1", "/local", "not a url", "", None]


class HostileContentTests(unittest.TestCase):
    """ADR-0037: agent-written text can never run in the dashboard, which would let it act with the person's session."""

    def scripts(self) -> list[Path]:
        return sorted(ASSETS.glob("*.js"))

    def test_no_script_turns_text_into_markup_or_code(self) -> None:
        sinks = r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|srcdoc|javascript:|setTimeout\(\s*[\"'`]"
        for script in self.scripts():
            with self.subTest(script=script.name):
                self.assertNotRegex(script.read_text(encoding="utf-8"), sinks)

    def test_every_link_is_an_internal_route_or_passes_the_https_check(self) -> None:
        for script in self.scripts():
            for target in re.findall(r"\.href\s*=\s*([^;]+);", script.read_text(encoding="utf-8")):
                with self.subTest(script=script.name, target=target):
                    self.assertRegex(target.strip(), r"^(`#/|safeLink\()")

    def test_the_pages_have_no_inline_scripts_or_event_handlers(self) -> None:
        for page in sorted(ASSETS.glob("*.html")):
            with self.subTest(page=page.name):
                html = page.read_text(encoding="utf-8")
                self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>")
                self.assertNotRegex(html, r"\son[a-z]+\s*=")

    @unittest.skipUnless(shutil.which("node"), "Node.js runs the link check when it is installed (TEST-4).")
    def test_safe_link_allows_only_https_urls(self) -> None:
        source = re.search(r"^function safeLink\(.*?^}", (ASSETS / "dashboard.js").read_text(encoding="utf-8"), re.M | re.S)
        self.assertIsNotNone(source, "dashboard.js defines safeLink")
        cases = HOSTILE_URLS + ["https://github.com/gdcruz0911/hearth/pull/68"]
        program = f"{source.group(0)}\nconsole.log(JSON.stringify({json.dumps(cases)}.map(safeLink)));"
        result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)

        self.assertEqual(json.loads(result.stdout), [None] * len(HOSTILE_URLS) + ["https://github.com/gdcruz0911/hearth/pull/68"])


@unittest.skipUnless(shutil.which("node"), "Node.js checks the dashboard's scripts when it is installed (TEST-4).")
class DashboardScriptSyntaxTests(unittest.TestCase):
    def test_every_dashboard_script_parses(self) -> None:
        # On 2026-10-07 a constant named like an existing function stopped dashboard.js from loading at all.
        for script in sorted((Path(__file__).parents[1] / "src/hearth/web_assets").glob("*.js")):
            with self.subTest(script=script.name):
                checked = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)


class SearchReportEndpointTests(unittest.TestCase):
    """The Knowledge tab asks through search_report, which keeps accepted evidence apart from what was only retrieved."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        note = root / "operations.md"
        note.write_text("# Operations\n\nThe deployment owner is Ada.\n", encoding="utf-8")
        self.service = HearthService(root / "hearth.sqlite")
        self.service.import_document(str(note))
        self.addCleanup(self.temporary_directory.cleanup)
        self.addCleanup(self.service.close)
        self.app = HearthWebApplication(self.service, "token")
        self.call = signed_in(self.app)

    def ask(self, payload: dict) -> tuple[int, dict]:
        response = self.call("POST", "/api/search/report", json.dumps(payload).encode())
        return response.status, json.loads(response.body)

    def test_an_answer_carries_accepted_evidence_with_freshness_and_the_gates_limit(self) -> None:
        status, report = self.ask({"question": "Who is the deployment owner?", "keyword": True})

        self.assertEqual((status, report["status"], report["retrieval"]["mode"]), (200, "supported", "keyword"))
        self.assertIn("Ada", report["evidence"][0]["excerpt"])
        self.assertEqual(report["evidence"][0]["source"], "current")
        self.assertIn("does not show that the evidence answers", report["gate"]["note"])
        self.assertTrue(all("excerpt" not in candidate for candidate in report["candidates"]))

    def test_an_empty_question_is_refused(self) -> None:
        status, body = self.ask({"question": "  "})

        self.assertEqual(status, 400)
        self.assertIn("Enter a question", body["error"])
