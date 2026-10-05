from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from hearth.domain import DocumentRelationship
from hearth.embedding import IndexBuildCancelled, IndexBuildStopped
from hearth.service import HearthService
from hearth.web import HearthWebApplication, HearthWebServer


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
        self.assertIn(b"Hearth", root_body)
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
        answer = self._json_request("POST", "api/search", {"question": "Who is the deployment owner?"})

        self.assertEqual(applied["action"], "reindex")
        self.assertEqual(reused_response.status, 409)
        self.assertIn(b"Preview the action again", reused)
        self.assertIn("Lin", answer["answer"]["text"])

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

        response = app.respond("GET", "/token/api/workbench/tasks", b"")
        outside = app.respond("GET", "/api/workbench/tasks", b"")

        self.assertEqual((response.status, json.loads(response.body)), (200, payload))
        self.assertEqual(outside.status, 404)

    def test_without_the_workbench_the_endpoint_says_so(self) -> None:
        response = HearthWebApplication(self.service, "token").respond("GET", "/token/api/workbench/tasks", b"")

        self.assertEqual(response.status, 404)
        self.assertIn("not connected", json.loads(response.body)["error"])

    def test_the_transcript_route_passes_task_run_and_offset_and_rejects_other_shapes(self) -> None:
        calls = []
        app = HearthWebApplication(self.service, "token", workbench_transcript=lambda *args: calls.append(args) or {"items": []})

        ok = app.respond("GET", "/token/api/workbench/transcript/20260930-120000/01-implement-claude/42", b"")
        bad = app.respond("GET", "/token/api/workbench/transcript/20260930-120000/01-implement-claude/x", b"")
        unknown = HearthWebApplication(self.service, "token", workbench_transcript=lambda *args: None).respond(
            "GET", "/token/api/workbench/transcript/20260930-120000/01-implement-claude/0", b"")

        self.assertEqual((ok.status, calls), (200, [("20260930-120000", "01-implement-claude", 42)]))
        self.assertEqual((bad.status, unknown.status), (400, 404))


class DashboardPageTests(unittest.TestCase):
    """The dashboard is the home page; the knowledge page moved to knowledge; nothing loads from another host (UI-4)."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.service = HearthService(Path(self.temporary_directory.name) / "hearth.sqlite")
        self.addCleanup(self.temporary_directory.cleanup)
        self.addCleanup(self.service.close)
        self.app = HearthWebApplication(self.service, "token")

    def test_the_home_page_is_the_dashboard_and_the_knowledge_page_moved(self) -> None:
        home = self.app.respond("GET", "/token/", b"")
        knowledge = self.app.respond("GET", "/token/knowledge", b"")

        self.assertIn(b"Needs you", home.body)
        self.assertIn(b'href="knowledge"', home.body)
        self.assertIn(b"map-surface", knowledge.body)

    def test_dashboard_assets_load_nothing_from_another_host_and_never_insert_html(self) -> None:
        for name, kind in (("assets/dashboard.css", "text/css"), ("assets/dashboard.js", "application/javascript")):
            with self.subTest(asset=name):
                response = self.app.respond("GET", f"/token/{name}", b"")
                self.assertEqual((response.status, response.content_type.split(";")[0]), (200, kind))
                self.assertNotRegex(response.body.decode(), r"https?://|@import|url\(")
        script = self.app.respond("GET", "/token/assets/dashboard.js", b"").body.decode()
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("insertAdjacentHTML", script)
