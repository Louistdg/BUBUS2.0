"""Local GET-only dashboard and isolated synthetic ingestion test API."""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from .contract import ContractError, parse_json_no_duplicates
from .store import IngestConflict, SyntheticProjectionStore


class Authenticator(Protocol):
    """Server-side auth seam; implementations return 401, 403, or None."""

    def authorize(self, header: str | None, audience: str) -> int | None: ...


class TestAuthenticator:
    """Explicit in-memory test identities; not a production authentication system."""

    def __init__(self, *, viewer_tokens: set[str], publisher_tokens: set[str], recognized_tokens: set[str] | None = None):
        self._viewer = set(viewer_tokens)
        self._publisher = set(publisher_tokens)
        self._recognized = set(recognized_tokens or ()) | self._viewer | self._publisher

    def authorize(self, header: str | None, audience: str) -> int | None:
        if not header or not header.startswith("Bearer "):
            return 401
        token = header[7:]
        if not any(secrets.compare_digest(token, candidate) for candidate in self._recognized):
            return 401
        allowed = self._viewer if audience == "viewer" else self._publisher
        if audience == "viewer" and token in self._publisher and token not in self._viewer:
            return 401
        if not any(secrets.compare_digest(token, candidate) for candidate in allowed):
            return 403
        return None


def make_server(store: SyntheticProjectionStore, authenticator: Authenticator, *, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    if host == "::1":
        raise ValueError("IPv6 loopback is unsupported by this IPv4 synthetic test server")
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("synthetic test server may bind only to IPv4 loopback")
    class Handler(BaseHTTPRequestHandler):
        server_version = "SyntheticOnly/1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send(self, status: int, body: dict) -> None:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _auth(self, audience: str) -> bool:
            status = authenticator.authorize(self.headers.get("Authorization"), audience)
            if status:
                self._send(status, {"error": "UNAUTHENTICATED" if status == 401 else "FORBIDDEN"})
                return False
            return True

        def do_GET(self) -> None:
            if not self._auth("viewer"):
                return
            parsed = urlsplit(self.path)
            if parsed.path not in {"/api/v1/synthetic/health", "/api/v1/synthetic/missions"}:
                self._send(404, {"error": "NOT_FOUND"})
                return
            try:
                metadata, snapshot = store.projection(after_read=lambda: None)
                if parsed.path == "/api/v1/synthetic/health":
                    self._send(200, metadata)
                    return
                if snapshot is None:
                    self._send(503, {"error": "UNAVAILABLE", "sync_state": "UNAVAILABLE", "dataset_mode": "SYNTHETIC_ONLY", "simulation_label": "SIMULATION"})
                    return
                query = parse_qs(parsed.query)
                try:
                    limit = int(query.get("limit", ["25"])[0])
                except ValueError:
                    self._send(422, {"error": "INVALID_LIMIT"})
                    return
                if limit < 1 or limit > 100:
                    self._send(422, {"error": "INVALID_LIMIT"})
                    return
                items = snapshot["payload"]["missions"][:limit]
                response = {"schema_version": 1, "dataset_mode": "SYNTHETIC_ONLY", "sync_state": metadata["sync_state"], "snapshot_sequence": snapshot["sequence"], "snapshot_hash": snapshot["snapshot_sha256"], "simulation_label": "SIMULATION", "items": items, "next_cursor": None}
                if metadata["snapshot_sequence"] != snapshot["sequence"] or metadata["snapshot_hash"] != snapshot["snapshot_sha256"]:
                    response["sync_state"] = "UNKNOWN"
                self._send(200, response)
            except Exception:
                self._send(503, {"error": "UNAVAILABLE", "sync_state": "UNAVAILABLE", "dataset_mode": "SYNTHETIC_ONLY", "simulation_label": "SIMULATION"})

        def do_POST(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path != "/internal/v1/synthetic/snapshots":
                length = int(self.headers.get("Content-Length", "0") or 0)
                if 0 < length <= 128 * 1024:
                    self.rfile.read(length)
                self._send(405, {"error": "METHOD_NOT_ALLOWED"})
                return
            if not self._auth("publisher"):
                length = int(self.headers.get("Content-Length", "0") or 0)
                if 0 < length <= 128 * 1024:
                    self.rfile.read(length)
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._send(415, {"error": "UNSUPPORTED_MEDIA_TYPE"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > 128 * 1024:
                    self._send(413, {"error": "PAYLOAD_TOO_LARGE"})
                    return
                body = parse_json_no_duplicates(self.rfile.read(length))
                result = store.ingest(body)
                self._send(200 if result["result"] == "IDEMPOTENT" else 201, result)
            except (ContractError, ValueError):
                self._send(422, {"error": "INVALID_SYNTHETIC_SNAPSHOT"})
            except IngestConflict as exc:
                self._send(409, {"error": exc.code})
            except Exception:
                self._send(503, {"error": "UNAVAILABLE", "sync_state": "UNAVAILABLE"})

        def do_PUT(self) -> None:
            self._send(405, {"error": "METHOD_NOT_ALLOWED"})

        def do_PATCH(self) -> None:
            self._send(405, {"error": "METHOD_NOT_ALLOWED"})

        def do_DELETE(self) -> None:
            self._send(405, {"error": "METHOD_NOT_ALLOWED"})

        def do_OPTIONS(self) -> None:
            self._send(405, {"error": "METHOD_NOT_ALLOWED"})

    return ThreadingHTTPServer((host, port), Handler)
