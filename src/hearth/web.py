from __future__ import annotations

import hmac
import json
import secrets
import subprocess
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

from .domain import (
    Answer,
    CollectionHealth,
    DocumentInspection,
    FileOrganizationError,
    FileOrganizationPlan,
    ImportError,
    ImportSummary,
    ImportedDocument,
    SourceRelinkError,
    SourceRelinkPlan,
)
from .service import HearthService


_ASSET_DIRECTORY = Path(__file__).with_name("web_assets")
_MAX_REQUEST_BODY_BYTES = 64 * 1024
_PREVIEW_LIFETIME_SECONDS = 5 * 60
_CONNECTION_TIMEOUT_SECONDS = 5


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
        choose_directory: Callable[[], Path] = None,
    ):
        self._service = service
        self._capability_token = capability_token
        self._choose_file = choose_file or choose_local_file
        self._choose_directory = choose_directory or choose_local_directory
        self._pending_actions: dict[str, _PendingAction] = {}

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
        except (FileOrganizationError, ImportError, SourceRelinkError, WebRequestError) as exc:
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
            document_id = _document_id(parts[2])
            payload = _json_body(body)
            return self._preview(document_id, parts[4], payload)
        if relative_path.startswith("api/previews/") and relative_path.endswith("/apply"):
            preview_id = relative_path.removeprefix("api/previews/").removesuffix("/apply")
            if not preview_id or "/" in preview_id:
                return self._not_found()
            return self._apply(preview_id)
        return self._not_found()

    def _preview(self, document_id: int, action: str, payload: Mapping[str, Any]) -> _WebResponse:
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
        if action == "organize":
            operation = payload.get("operation")
            if operation == "rename":
                rename = payload.get("rename")
                if not isinstance(rename, str):
                    raise WebRequestError("Enter one new file name before previewing a rename.")
                plan = self._service.plan_organization(document_id, rename=rename)
                arguments = {"rename": rename}
            elif operation == "move":
                move_to = self._choose_directory()
                plan = self._service.plan_organization(document_id, move_to=move_to)
                arguments = {"move_to": str(move_to)}
            else:
                raise WebRequestError("Choose either a rename or a move.")
            preview = self._create_preview(action, document_id, arguments)
            return self._json_response({"preview": {**preview, **_organization_plan_payload(plan)}})
        if action == "relink":
            plan = self._service.plan_relink(document_id, self._choose_file())
            preview = self._create_preview(action, document_id, {"replacement_path": str(plan.replacement_source_path)})
            return self._json_response({"preview": {**preview, **_relink_plan_payload(plan)}})
        return self._not_found()

    def _apply(self, preview_id: str) -> _WebResponse:
        self._discard_expired_previews()
        pending = self._pending_actions.pop(preview_id, None)
        if pending is None:
            return self._json_error(
                HTTPStatus.CONFLICT,
                "This preview is no longer available. Preview the action again before applying it.",
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
        if pending.action == "organize":
            if "rename" in pending.arguments:
                plan = self._service.apply_organization(pending.document_id, rename=pending.arguments["rename"])
            else:
                plan = self._service.apply_organization(
                    pending.document_id, move_to=Path(pending.arguments["move_to"])
                )
            return self._json_response({"applied": {"action": "organize", **_organization_plan_payload(plan)}})
        if pending.action == "relink":
            plan = self._service.apply_relink(pending.document_id, Path(pending.arguments["replacement_path"]))
            return self._json_response({"applied": {"action": "relink", **_relink_plan_payload(plan)}})
        return self._not_found()

    def _create_preview(self, action: str, document_id: int, arguments: Mapping[str, str]) -> dict[str, str]:
        preview_id = secrets.token_urlsafe(18)
        self._pending_actions[preview_id] = _PendingAction(action, document_id, arguments, time.monotonic())
        return {"id": preview_id, "action": action}

    def _discard_expired_previews(self) -> None:
        cutoff = time.monotonic() - _PREVIEW_LIFETIME_SECONDS
        for preview_id, pending in tuple(self._pending_actions.items()):
            if pending.created_at < cutoff:
                del self._pending_actions[preview_id]

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
        choose_directory: Callable[[], Path] = None,
        browser_opener: Callable[[str], bool] = webbrowser.open,
    ):
        token = secrets.token_urlsafe(32)
        self._application = HearthWebApplication(service, token, choose_file, choose_directory)
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
        if self._serving.is_set():
            self._http_server.shutdown()
        self._http_server.server_close()


def choose_local_file() -> Path:
    return _choose_with_osascript(
        'POSIX path of (choose file with prompt "Choose a local document to import into Hearth")'
    )


def choose_local_directory() -> Path:
    return _choose_with_osascript(
        'POSIX path of (choose folder with prompt "Choose a local destination folder")'
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


def _document_id(raw_value: str) -> int:
    try:
        document_id = int(raw_value)
    except ValueError as exc:
        raise WebRequestError("The document ID is invalid.") from exc
    if document_id < 1:
        raise WebRequestError("The document ID is invalid.")
    return document_id


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


def _organization_plan_payload(plan: FileOrganizationPlan) -> dict[str, Any]:
    return {
        "document": _document_payload(plan.document),
        "operation": plan.operation,
        "source_path": str(plan.source_path),
        "target_path": str(plan.target_path),
        "message": "No changes have been made. Apply this exact preview to continue.",
    }


def _relink_plan_payload(plan: SourceRelinkPlan) -> dict[str, Any]:
    return {
        "document": _document_payload(plan.document),
        "operation": "relink",
        "previous_source_path": str(plan.previous_source_path),
        "replacement_source_path": str(plan.replacement_source_path),
        "message": "No changes have been made. Apply this exact preview to continue.",
    }
