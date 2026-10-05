from __future__ import annotations

import hmac
import json
import resource
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .embedding import IndexBuildCancelled, IndexBusy, IndexBuildStopped
from .domain import (
    Answer,
    CollectionHealth,
    Chunk,
    DocumentRelationship,
    DocumentInspection,
    ImportError,
    ImportSummary,
    ImportedDocument,
    SourceImportPlan,
    SourceImportResult,
    SourceCandidate,
    SourceRoot,
)
from .service import HearthService


_ASSET_DIRECTORY = Path(__file__).with_name("web_assets")
_MAX_REQUEST_BODY_BYTES = 64 * 1024
_PREVIEW_LIFETIME_SECONDS = 5 * 60
_CONNECTION_TIMEOUT_SECONDS = 5
_MAP_RELATIONSHIP_LIMIT = 120


class NativeChooserCancelled(ValueError):
    """Raised when a user dismisses a native local-file chooser."""


class WebRequestError(ValueError):
    """Raised for malformed browser input without exposing server internals."""


@dataclass(frozen=True)
class _PendingAction:
    action: str
    document_id: int
    arguments: Mapping[str, str]
    created_at: float


@dataclass(frozen=True)
class _PendingSourceImport:
    plan: SourceImportPlan
    created_at: float


@dataclass
class _SemanticIndexJob:
    total: int
    completed: int
    status: str
    cancellation_requested: threading.Event
    started_at: float
    cpu_started_at: float
    phase: str = "preparing local model"
    semantic_index_status: str | None = None
    error: str | None = None
    warning: str | None = None


@dataclass(frozen=True)
class _WebResponse:
    status: HTTPStatus
    content_type: str
    body: bytes


class HearthWebApplication:
    """Routes the local browser surface onto existing HearthService operations."""

    def __init__(
        self,
        service: HearthService,
        capability_token: str,
        choose_file: Callable[[], Path] = None,
        source_roots: tuple[Path, ...] = (),
        workbench_tasks: Callable[[], Mapping[str, Any]] | None = None,
        workbench_transcript: Callable[[str, str, int], Mapping[str, Any] | None] | None = None,
    ):
        self._service = service
        self._workbench_transcript = workbench_transcript
        self._capability_token = capability_token
        # Passed in by the CLI, the composition root, so this module never imports the workbench (CODE-1).
        self._workbench_tasks = workbench_tasks
        self._choose_file = choose_file or choose_local_file
        self._source_roots = source_roots
        self._pending_actions: dict[str, _PendingAction] = {}
        self._pending_source_imports: dict[str, _PendingSourceImport] = {}
        self._semantic_index_job: _SemanticIndexJob | None = None
        self._semantic_index_job_lock = threading.Lock()

    @property
    def base_path(self) -> str:
        return f"/{self._capability_token}/"

    def respond(self, method: str, raw_path: str, body: bytes) -> _WebResponse:
        path = urlsplit(raw_path).path
        if not path.startswith(self.base_path):
            return self._not_found()
        relative_path = path[len(self.base_path) :]
        try:
            if method == "GET":
                return self._get(relative_path)
            if method == "POST":
                return self._post(relative_path, body)
            return self._json_error(HTTPStatus.METHOD_NOT_ALLOWED, "This local endpoint does not allow that method.")
        except NativeChooserCancelled:
            return self._json_error(HTTPStatus.CONFLICT, "No local file or folder was selected.")
        except (ImportError, WebRequestError) as exc:
            return self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
        except (OSError, ValueError):
            return self._json_error(HTTPStatus.BAD_REQUEST, "The requested local action could not be completed.")
        except Exception:
            return self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Hearth could not complete that local action.")

    def _get(self, relative_path: str) -> _WebResponse:
        if relative_path == "":
            return self._asset("index.html", "text/html; charset=utf-8")
        if relative_path == "assets/app.css":
            return self._asset("app.css", "text/css; charset=utf-8")
        if relative_path == "assets/app.js":
            return self._asset("app.js", "application/javascript; charset=utf-8")
        if relative_path == "api/health":
            return self._json_response(_health_payload(self._service.collection_health()))
        if relative_path == "api/documents":
            return self._json_response({"documents": [_document_payload(item) for item in self._service.list_documents()]})
        if relative_path == "api/map":
            return self._json_response(_collection_map_payload(self._service))
        if relative_path == "api/sources":
            return self._json_response({"roots": [_source_root_payload(item) for item in self._service.source_roots(self._source_roots)]})
        if relative_path == "api/semantic-index":
            return self._json_response({"job": self._semantic_index_job_payload()})
        if relative_path == "api/workbench/tasks":
            if self._workbench_tasks is None:
                return self._json_error(HTTPStatus.NOT_FOUND, "The workbench is not connected to this interface.")
            return self._json_response(self._workbench_tasks())
        if relative_path.startswith("api/workbench/transcript/"):
            if self._workbench_transcript is None:
                return self._json_error(HTTPStatus.NOT_FOUND, "The workbench is not connected to this interface.")
            parts = relative_path.removeprefix("api/workbench/transcript/").split("/")
            if len(parts) != 3 or not parts[2].isdigit():
                return self._json_error(HTTPStatus.BAD_REQUEST, "Ask for a transcript as task/run/offset.")
            found = self._workbench_transcript(parts[0], parts[1], int(parts[2]))
            if found is None:
                return self._json_error(HTTPStatus.NOT_FOUND, "No such task.")
            return self._json_response(found)
        if relative_path.startswith("api/relationships/"):
            left_document_id, right_document_id = _relationship_document_ids(
                relative_path.removeprefix("api/relationships/")
            )
            relationship = next(
                (
                    item
                    for item in self._service.document_relationships(limit=_MAP_RELATIONSHIP_LIMIT)
                    if (item.left_chunk.document_id, item.right_chunk.document_id)
                    == (left_document_id, right_document_id)
                ),
                None,
            )
            if relationship is None:
                return self._json_error(HTTPStatus.NOT_FOUND, "No current semantic relationship has those sources.")
            return self._json_response({"relationship": _relationship_payload(relationship)})
        if relative_path.startswith("api/documents/"):
            document_id = _document_id(relative_path.removeprefix("api/documents/"))
            inspection = self._service.inspect_document(document_id)
            if inspection is None:
                return self._json_error(HTTPStatus.NOT_FOUND, "No imported document has that ID.")
            return self._json_response(_inspection_payload(inspection))
        return self._not_found()

    def _post(self, relative_path: str, body: bytes) -> _WebResponse:
        if relative_path == "api/import":
            summary = self._service.import_with_summary(str(self._choose_file()))
            return self._json_response({"import": _summary_payload(summary)})
        if relative_path == "api/sources/preview":
            return self._source_import_preview()
        if relative_path == "api/semantic-index/preview":
            return self._semantic_index_preview()
        if relative_path == "api/semantic-index/cancel":
            return self._json_response({"job": self._cancel_semantic_index_rebuild()})
        if relative_path.startswith("api/source-previews/") and relative_path.endswith("/apply"):
            preview_id = relative_path.removeprefix("api/source-previews/").removesuffix("/apply")
            if not preview_id or "/" in preview_id:
                return self._not_found()
            return self._apply_source_import(preview_id)
        if relative_path == "api/search":
            payload = _json_body(body)
            question = payload.get("question")
            if not isinstance(question, str) or not question.strip():
                raise WebRequestError("Enter a question before searching the local collection.")
            return self._json_response({"answer": _answer_payload(self._service.answer(question))})
        if relative_path.startswith("api/documents/") and relative_path.endswith("/preview"):
            prefix = relative_path.removesuffix("/preview")
            parts = prefix.split("/")
            if len(parts) != 5 or parts[:2] != ["api", "documents"] or parts[3] != "actions":
                return self._not_found()
            return self._preview(_document_id(parts[2]), parts[4])
        if relative_path.startswith("api/previews/") and relative_path.endswith("/apply"):
            preview_id = relative_path.removeprefix("api/previews/").removesuffix("/apply")
            if not preview_id or "/" in preview_id:
                return self._not_found()
            return self._apply(preview_id)
        return self._not_found()

    def _preview(self, document_id: int, action: str) -> _WebResponse:
        self._discard_expired_previews()
        if action == "reindex":
            document = self._service.plan_reindex_document(document_id)
            preview = self._create_preview(action, document_id, {})
            return self._json_response(
                {
                    "preview": {
                        **preview,
                        "document": _document_payload(document),
                        "message": "Hearth will replace this document's derived pages and chunks from its current local source.",
                    }
                }
            )
        if action == "remove":
            document = self._service.plan_remove_document(document_id)
            preview = self._create_preview(action, document_id, {})
            return self._json_response(
                {
                    "preview": {
                        **preview,
                        "document": _document_payload(document),
                        "message": "Hearth will remove this local collection record and its derived index data. The source file will stay untouched.",
                    }
                }
            )
        return self._not_found()

    def _apply(self, preview_id: str) -> _WebResponse:
        self._discard_expired_previews()
        pending = self._pending_actions.pop(preview_id, None)
        if pending is None:
            return self._json_error(
                HTTPStatus.CONFLICT,
                "This preview is no longer available. Preview the action again before applying it.",
            )
        if pending.action == "semantic-index":
            return self._json_response(
                {
                    "applied": {
                        "action": "semantic-index",
                        "job": self._start_semantic_index_rebuild(),
                        "message": "Hearth started a local semantic-index rebuild. You can keep using the interface while it runs.",
                    }
                }
            )
        if pending.action == "reindex":
            summary = self._service.reindex_document_by_id(pending.document_id)
            return self._json_response({"applied": {"action": "reindex", "import": _summary_payload(summary)}})
        if pending.action == "remove":
            self._service.remove_document_by_id(pending.document_id)
            return self._json_response(
                {
                    "applied": {
                        "action": "remove",
                        "message": "The local collection record was removed. The source file was not changed.",
                    }
                }
            )
        return self._not_found()

    def _semantic_index_preview(self) -> _WebResponse:
        self._discard_expired_previews()
        health = self._service.collection_health()
        if health.semantic_index_status == "not configured":
            raise WebRequestError("Start Hearth with a local embedding model and index directory before building the semantic map.")
        with self._semantic_index_job_lock:
            if self._semantic_index_job is not None and self._semantic_index_job.status in {"running", "cancelling"}:
                raise WebRequestError("A local semantic-index rebuild is already running.")
        preview = self._create_preview("semantic-index", 0, {})
        return self._json_response(
            {
                "preview": {
                    **preview,
                    "message": (
                        f"Hearth will rebuild local embeddings from {health.chunk_count} evidence units. "
                        "Original files will not be read or changed beyond the existing imported evidence."
                    ),
                }
            }
        )

    def cancel_semantic_index_rebuild(self) -> None:
        """Ask an active local rebuild to stop at its next small batch boundary."""
        with self._semantic_index_job_lock:
            if self._semantic_index_job is not None and self._semantic_index_job.status in {"running", "cancelling"}:
                self._semantic_index_job.cancellation_requested.set()
                self._semantic_index_job.status = "cancelling"

    def _start_semantic_index_rebuild(self) -> dict[str, Any]:
        health = self._service.collection_health()
        with self._semantic_index_job_lock:
            if self._semantic_index_job is not None and self._semantic_index_job.status in {"running", "cancelling"}:
                raise WebRequestError("A local semantic-index rebuild is already running.")
            job = _SemanticIndexJob(
                total=health.chunk_count,
                completed=0,
                status="running",
                cancellation_requested=threading.Event(),
                started_at=time.monotonic(),
                cpu_started_at=time.process_time(),
            )
            self._semantic_index_job = job
        thread = threading.Thread(target=self._run_semantic_index_rebuild, args=(job,), daemon=True)
        thread.start()
        return self._semantic_index_job_payload()

    def _run_semantic_index_rebuild(self, job: _SemanticIndexJob) -> None:
        def on_progress(completed: int, total: int) -> None:
            with self._semantic_index_job_lock:
                if self._semantic_index_job is job:
                    job.completed = completed
                    job.total = total
                    job.phase = "finalizing derived index" if completed >= total else "embedding evidence"

        def on_warning(message: str) -> None:
            with self._semantic_index_job_lock:
                if self._semantic_index_job is job:
                    job.warning = message

        try:
            semantic_index_status = self._service.rebuild_semantic_index(
                on_progress=on_progress,
                is_cancelled=job.cancellation_requested.is_set,
                on_warning=on_warning,
            )
        except IndexBuildCancelled:
            with self._semantic_index_job_lock:
                if self._semantic_index_job is job:
                    job.status = "cancelled"
                    job.phase = "cancelled"
        except Exception as exc:
            with self._semantic_index_job_lock:
                if self._semantic_index_job is job:
                    job.status = "failed"
                    job.phase = "failed"
                    job.error = (
                        str(exc)
                        if isinstance(exc, (IndexBusy, IndexBuildStopped))
                        else "The local semantic-index rebuild stopped before completion."
                    )
        else:
            with self._semantic_index_job_lock:
                if self._semantic_index_job is job:
                    job.completed = job.total
                    job.status = "completed"
                    job.phase = "ready"
                    job.semantic_index_status = semantic_index_status

    def _cancel_semantic_index_rebuild(self) -> dict[str, Any]:
        with self._semantic_index_job_lock:
            job = self._semantic_index_job
            if job is None or job.status not in {"running", "cancelling"}:
                raise WebRequestError("No local semantic-index rebuild is running.")
            job.cancellation_requested.set()
            job.status = "cancelling"
            job.phase = "stopping after the current batch"
        return self._semantic_index_job_payload()

    def _semantic_index_job_payload(self) -> dict[str, Any]:
        with self._semantic_index_job_lock:
            job = self._semantic_index_job
            if job is None:
                return {"status": "idle", "completed": 0, "total": 0}
            elapsed_seconds = max(time.monotonic() - job.started_at, 0.0)
            cpu_seconds = max(time.process_time() - job.cpu_started_at, 0.0)
            throughput_per_minute = (job.completed / elapsed_seconds * 60) if elapsed_seconds and job.completed else 0.0
            return {
                "status": job.status,
                "completed": job.completed,
                "total": job.total,
                "phase": job.phase,
                "semantic_index_status": job.semantic_index_status,
                "error": job.error,
                "warning": job.warning,
                "benchmark": {
                    "elapsed_seconds": round(elapsed_seconds, 2),
                    "cpu_seconds": round(cpu_seconds, 2),
                    "peak_resident_memory_bytes": _process_peak_resident_memory_bytes(),
                    "evidence_units_per_minute": round(throughput_per_minute, 1),
                },
            }

    def _source_import_preview(self) -> _WebResponse:
        self._discard_expired_previews()
        plan = self._service.plan_source_import(self._source_roots)
        preview_id = secrets.token_urlsafe(18)
        self._pending_source_imports[preview_id] = _PendingSourceImport(plan, time.monotonic())
        return self._json_response(
            {
                "preview": {
                    "id": preview_id,
                    "action": "import sources",
                    "roots": [_source_root_payload(item) for item in plan.roots],
                    "candidates": [_source_candidate_payload(item) for item in plan.candidates[:20]],
                    "candidate_count": len(plan.candidates),
                    "additional_candidate_count": max(len(plan.candidates) - 20, 0),
                    "message": "No files have been imported. Review the eligible files, then approve this exact scan.",
                }
            }
        )

    def _apply_source_import(self, preview_id: str) -> _WebResponse:
        self._discard_expired_previews()
        pending = self._pending_source_imports.pop(preview_id, None)
        if pending is None:
            return self._json_error(
                HTTPStatus.CONFLICT,
                "This source preview is no longer available. Scan the connected folders again before importing.",
            )
        return self._json_response({"applied": {"action": "import sources", **_source_import_result_payload(self._service.import_source_plan(pending.plan))}})

    def _create_preview(self, action: str, document_id: int, arguments: Mapping[str, str]) -> dict[str, str]:
        preview_id = secrets.token_urlsafe(18)
        self._pending_actions[preview_id] = _PendingAction(action, document_id, arguments, time.monotonic())
        return {"id": preview_id, "action": action}

    def _discard_expired_previews(self) -> None:
        cutoff = time.monotonic() - _PREVIEW_LIFETIME_SECONDS
        for preview_id, pending in tuple(self._pending_actions.items()):
            if pending.created_at < cutoff:
                del self._pending_actions[preview_id]
        for preview_id, pending in tuple(self._pending_source_imports.items()):
            if pending.created_at < cutoff:
                del self._pending_source_imports[preview_id]

    def _asset(self, name: str, content_type: str) -> _WebResponse:
        try:
            content = (_ASSET_DIRECTORY / name).read_bytes()
        except OSError:
            return self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Hearth web assets are unavailable.")
        return _WebResponse(HTTPStatus.OK, content_type, content)

    @staticmethod
    def _json_response(payload: Mapping[str, Any]) -> _WebResponse:
        return _WebResponse(
            HTTPStatus.OK,
            "application/json; charset=utf-8",
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )

    @staticmethod
    def _json_error(status: HTTPStatus, message: str) -> _WebResponse:
        return _WebResponse(
            status,
            "application/json; charset=utf-8",
            json.dumps({"error": message}, separators=(",", ":")).encode("utf-8"),
        )

    def _not_found(self) -> _WebResponse:
        return self._json_error(HTTPStatus.NOT_FOUND, "This local Hearth page is unavailable.")


class HearthWebServer:
    """Single-process loopback server for Hearth's local browser interface."""

    def __init__(
        self,
        service: HearthService,
        *,
        port: int,
        choose_file: Callable[[], Path] = None,
        source_roots: tuple[Path, ...] = (),
        browser_opener: Callable[[str], bool] = webbrowser.open,
        workbench_tasks: Callable[[], Mapping[str, Any]] | None = None,
        workbench_transcript: Callable[[str, str, int], Mapping[str, Any] | None] | None = None,
    ):
        token = secrets.token_urlsafe(32)
        self._application = HearthWebApplication(service, token, choose_file, source_roots, workbench_tasks, workbench_transcript)
        self._browser_opener = browser_opener
        self._http_server = _LoopbackHTTPServer(("127.0.0.1", port), _handler_type(self._application))
        self._http_server.timeout = 0.5
        self._serving = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._http_server.server_port}{self._application.base_path}"

    def open_browser(self) -> bool:
        return self._browser_opener(self.url)

    def serve_forever(self) -> None:
        self._serving.set()
        try:
            self._http_server.serve_forever()
        finally:
            self._serving.clear()

    def close(self) -> None:
        self._application.cancel_semantic_index_rebuild()
        if self._serving.is_set():
            self._http_server.shutdown()
        self._http_server.server_close()


def choose_local_file() -> Path:
    return _choose_with_osascript(
        'POSIX path of (choose file with prompt "Choose a local document to import into Hearth")'
    )


def _choose_with_osascript(script: str) -> Path:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise ImportError("The local file chooser is available only on macOS.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ImportError("The local file chooser timed out before a selection was made.") from exc
    if result.returncode != 0:
        if "User canceled" in result.stderr or "User cancelled" in result.stderr:
            raise NativeChooserCancelled()
        raise ImportError("The local file chooser could not complete.")
    selected_path = result.stdout.strip()
    if not selected_path:
        raise NativeChooserCancelled()
    return Path(selected_path)


def _handler_type(application: HearthWebApplication) -> type[BaseHTTPRequestHandler]:
    class HearthWebRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler protocol
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler protocol
            self._respond("POST")

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler protocol
            self._respond("HEAD")

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler protocol
            self._respond("OPTIONS")

        def log_message(self, format: str, *args: object) -> None:
            """Keep capability URLs and local paths out of terminal logs."""

        def handle(self) -> None:
            try:
                super().handle()
            except TimeoutError:
                return

        def _respond(self, method: str) -> None:
            if not self._has_expected_host():
                response = HearthWebApplication._json_error(
                    HTTPStatus.BAD_REQUEST, "Hearth accepts only its loopback host."
                )
            else:
                application_method = "GET" if method == "HEAD" else method
                response = application.respond(
                    application_method, self.path, self._request_body() if method == "POST" else b""
                )
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(response.body)

        def _has_expected_host(self) -> bool:
            expected_host = f"127.0.0.1:{self.server.server_port}"
            return hmac.compare_digest(self.headers.get("Host", ""), expected_host)

        def _request_body(self) -> bytes:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError:
                raise WebRequestError("The local request length is invalid.") from None
            if length < 0 or length > _MAX_REQUEST_BODY_BYTES:
                raise WebRequestError("The local request is too large.")
            return self.rfile.read(length)

    return HearthWebRequestHandler


class _LoopbackHTTPServer(HTTPServer):
    """Avoid letting an incomplete loopback connection block the local UI indefinitely."""

    def get_request(self) -> tuple[Any, Any]:
        request, client_address = super().get_request()
        request.settimeout(_CONNECTION_TIMEOUT_SECONDS)
        return request, client_address


def _json_body(body: bytes) -> Mapping[str, Any]:
    if not body:
        return {}
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebRequestError("The local request must contain valid JSON.") from exc
    if not isinstance(value, dict):
        raise WebRequestError("The local request must contain an object.")
    return value


def _process_peak_resident_memory_bytes() -> int:
    """Return the peak resident size for the local Hearth process in bytes."""
    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(maximum if sys.platform == "darwin" else maximum * 1024)


def _document_id(raw_value: str) -> int:
    try:
        document_id = int(raw_value)
    except ValueError as exc:
        raise WebRequestError("The document ID is invalid.") from exc
    if document_id < 1:
        raise WebRequestError("The document ID is invalid.")
    return document_id


def _relationship_document_ids(raw_value: str) -> tuple[int, int]:
    parts = raw_value.split("/")
    if len(parts) != 2:
        raise WebRequestError("The relationship source IDs are invalid.")
    left_document_id, right_document_id = (_document_id(part) for part in parts)
    if left_document_id >= right_document_id:
        raise WebRequestError("The relationship source IDs are invalid.")
    return left_document_id, right_document_id


def _document_payload(document: ImportedDocument) -> dict[str, Any]:
    return {
        "id": document.id,
        "name": document.name,
        "page_count": document.page_count,
        "chunk_count": document.chunk_count,
        "ocr_page_count": document.ocr_page_count,
    }


def _summary_payload(summary: ImportSummary) -> dict[str, Any]:
    return {
        "document": _document_payload(summary.document),
        "semantic_index_status": summary.semantic_index_status,
        "ocr_artifact_status": summary.ocr_artifact_status,
    }


def _source_root_payload(root: SourceRoot) -> dict[str, Any]:
    return {
        "name": root.name,
        "status": root.status,
        "candidate_count": root.candidate_count,
        "imported_count": root.imported_count,
    }


def _source_candidate_payload(candidate: SourceCandidate) -> dict[str, str]:
    return {"name": candidate.path.name, "source_root": candidate.source_root}


def _source_import_result_payload(result: SourceImportResult) -> dict[str, Any]:
    return {
        "imported": [_summary_payload(item) for item in result.imported],
        "failures": [{"name": item.name, "message": item.message} for item in result.failures],
        "message": (
            f"Imported {len(result.imported)} files."
            if not result.failures
            else f"Imported {len(result.imported)} files. {len(result.failures)} files need review."
        ),
    }


def _health_payload(health: CollectionHealth) -> dict[str, Any]:
    return {
        "document_count": health.document_count,
        "page_count": health.page_count,
        "chunk_count": health.chunk_count,
        "ocr_page_count": health.ocr_page_count,
        "unavailable_source_count": health.unavailable_source_count,
        "changed_source_count": health.changed_source_count,
        "baseline_reindex_count": health.baseline_reindex_count,
        "semantic_index_status": health.semantic_index_status,
        "source_attention": [
            {"document_id": item.document_id, "document_name": item.document_name, "status": item.status}
            for item in health.source_attention
        ],
    }


def _collection_map_payload(service: HearthService) -> dict[str, Any]:
    """Expose only persisted document structure for the browser's relationship map.

    Matching section labels provide an honest, inspectable initial connection between
    documents. The map intentionally does not claim semantic similarity until the
    service has a dedicated, explainable relationship model.
    """

    documents = service.list_documents()
    semantic_relationships = service.document_relationships(limit=_MAP_RELATIONSHIP_LIMIT)
    clusters: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    for document in documents:
        inspection = service.inspect_document(document.id)
        if inspection is None:
            continue
        document_cluster_ids: set[str] = set()
        for page in inspection.pages:
            section = page.section.strip() if page.section else "Unsectioned material"
            cluster_id = f"section:{section.casefold()}"
            cluster = clusters.setdefault(
                cluster_id,
                {"id": cluster_id, "label": section, "document_ids": set()},
            )
            cluster["document_ids"].add(document.id)
            document_cluster_ids.add(cluster_id)
        edges.extend(
            {"document_id": document.id, "cluster_id": cluster_id}
            for cluster_id in sorted(document_cluster_ids)
        )

    return {
        "documents": [_document_payload(document) for document in documents],
        "clusters": [
            {
                "id": cluster["id"],
                "label": cluster["label"],
                "document_count": len(cluster["document_ids"]),
            }
            for cluster in sorted(clusters.values(), key=lambda item: (item["label"].casefold(), item["id"]))
        ],
        "edges": edges,
        "semantic_edges": [
            {
                "left_document_id": relationship.left_chunk.document_id,
                "right_document_id": relationship.right_chunk.document_id,
                "score": round(relationship.score, 4),
            }
            for relationship in semantic_relationships
        ],
    }


def _relationship_payload(relationship: DocumentRelationship) -> dict[str, Any]:
    return {
        "left_document": {"id": relationship.left_chunk.document_id, "name": relationship.left_chunk.document_name},
        "right_document": {"id": relationship.right_chunk.document_id, "name": relationship.right_chunk.document_name},
        "score": round(relationship.score, 4),
        "evidence": [_relationship_evidence_payload(relationship.left_chunk), _relationship_evidence_payload(relationship.right_chunk)],
    }


def _relationship_evidence_payload(chunk: Chunk) -> dict[str, Any]:
    return {
        "document_id": chunk.document_id,
        "document_name": chunk.document_name,
        "page_number": chunk.page_number,
        "section": chunk.section,
        "chunk_id": chunk.id,
        "quote": chunk.text,
    }


def _inspection_payload(inspection: DocumentInspection) -> dict[str, Any]:
    return {
        "document": _document_payload(inspection.document),
        "pages": [
            {
                "page_number": page.page_number,
                "section": page.section,
                "extraction_method": page.extraction_method,
                "ocr_confidence": page.ocr_confidence,
                "chunks": [
                    {"id": chunk.id, "char_start": chunk.char_start, "char_end": chunk.char_end}
                    for chunk in page.chunks
                ],
            }
            for page in inspection.pages
        ],
    }


def _answer_payload(answer: Answer) -> dict[str, Any]:
    return {
        "status": answer.status,
        "text": answer.text,
        "citations": [
            {
                "document_name": citation.document_name,
                "page_number": citation.page_number,
                "section": citation.section,
                "chunk_id": citation.chunk_id,
                "quote": citation.quote,
                "extraction_method": citation.extraction_method,
                "ocr_confidence": citation.ocr_confidence,
            }
            for citation in answer.citations
        ],
    }
