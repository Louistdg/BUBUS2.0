"""Authenticated, read-only Railway preview of public BUBUS2.0 markets."""
from __future__ import annotations

import base64
import hmac
import os
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

from bubus.server import ASSETS, DeskHandler, make_server


_PUBLIC_GET_PATHS = frozenset({
    "/", "/assets/desk.css", "/assets/desk.js", "/assets/icon.svg",
    "/api/desk/state", "/api/market-data/crypto-overview",
    "/api/market-data/coingecko-overview", "/api/market-data/state",
    "/api/desk/candles", "/api/desk/research",
})


class AuthenticatedCloudPreviewHandler(DeskHandler):
    """Only public market observations; no account, secret, or write routes."""

    def _authorized(self) -> bool:
        if (os.environ.get("BUBUS_PREVIEW_REQUIRE_HTTPS", "true").casefold() == "true"
                and self.headers.get("X-Forwarded-Proto", "").casefold() != "https"
                and self.client_address[0] not in {"127.0.0.1", "::1"}):
            return False
        username = os.environ.get("BUBUS_PREVIEW_USERNAME", "")
        password = os.environ.get("BUBUS_PREVIEW_PASSWORD", "")
        if not username or len(password) < 24:
            return False
        header = self.headers.get("Authorization", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.casefold() != "basic" or not encoded or len(encoded) > 2048:
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            submitted_user, separator, submitted_password = decoded.partition(":")
        except (ValueError, UnicodeError):
            return False
        return bool(separator) and hmac.compare_digest(submitted_user, username) and hmac.compare_digest(
            submitted_password, password)

    def _auth_required(self) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="BUBUS2.0 preview", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    # The inherited desk routes are local-only by design. This handler is
    # reachable only after authentication and only for the explicit GET list.
    def _local_host(self) -> bool:
        return True

    def _same_origin_read(self) -> bool:
        return True

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/healthz":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self._security_headers()
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if not self._authorized():
            return self._auth_required()
        path = urlsplit(self.path).path
        if path not in _PUBLIC_GET_PATHS:
            return self._send_json(404, {"error": "Cette route privée n’existe pas dans l’aperçu cloud."})
        if path == "/":
            try:
                body = (ASSETS / "index.html").read_bytes()
            except OSError:
                return self._send_json(503, {"error": "Interface indisponible."})
            banner = ('<div class="cloud-preview-banner" role="status">'
                      'APER\u00c7U CLOUD PROT\u00c9G\u00c9 · March\u00e9s publics en lecture seule '
                      '· Portefeuille priv\u00e9 et ordres indisponibles</div>').encode("utf-8")
            body = body.replace(b"<body>", b"<body>" + banner, 1)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def _deny_write(self) -> None:
        if not self._authorized():
            return self._auth_required()
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def do_POST(self) -> None:
        self._deny_write()

    def do_PUT(self) -> None:
        self._deny_write()

    def do_PATCH(self) -> None:
        self._deny_write()

    def do_DELETE(self) -> None:
        self._deny_write()

def main() -> int:
    if not os.environ.get("BUBUS_PREVIEW_USERNAME") or len(os.environ.get("BUBUS_PREVIEW_PASSWORD", "")) < 24:
        raise SystemExit("BUBUS preview requires an account and a password of at least 24 characters.")
    try:
        port = int(os.environ.get("PORT", "8790"))
    except ValueError:
        raise SystemExit("PORT must be a valid TCP port.") from None
    data_dir = Path(os.environ.get("BUBUS_PREVIEW_DATA_DIR", "/tmp/bubus-preview"))
    server = make_server(port, data_dir=data_dir, bind_host="0.0.0.0",
                         handler_class=AuthenticatedCloudPreviewHandler)
    print("BUBUS2.0 authenticated public-market preview is running.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
