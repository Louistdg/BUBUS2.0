"""Opt-in, read-only Client Portal market-data transport.

No request is made at import or construction time. The only host this module
can contact is the user's local IBKR Client Portal Gateway. It never exposes
order endpoints, stores credentials, or authorizes trading.
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import time
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any

from invest_ai.data.ibkr_stream import (
    IBKRMarketUpdate,
    build_subscription_message,
    build_unsubscription_message,
    parse_market_data_message,
)


_TICKLE_URL = "https://localhost:5000/v1/api/tickle"
_WEBSOCKET_URL = "wss://localhost:5000/v1/api/ws"
_MAX_TICKLE_BYTES = 32 * 1024
_SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


class IBKRClientPortalError(RuntimeError):
    """A safe, user-facing Client Portal connection error."""


class IBKRAuthenticationRequired(IBKRClientPortalError):
    """The user must authenticate manually in Client Portal Gateway."""


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the session request pinned to localhost even if Gateway misbehaves."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class IBKRClientPortalSession:
    """Calls only the fixed localhost /tickle endpoint with verified TLS."""

    def __init__(self, ca_file: str | Path) -> None:
        path = Path(ca_file).expanduser().resolve()
        if not path.is_file():
            raise IBKRClientPortalError("A local trusted certificate bundle is required.")
        self._ca_file = path
        self._session_token: str | None = None

    def _verified_tls_context(self) -> ssl.SSLContext:
        try:
            context = ssl.create_default_context(cafile=str(self._ca_file))
        except Exception:
            raise IBKRClientPortalError("The configured local certificate bundle is invalid.") from None
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise IBKRClientPortalError("TLS certificate and hostname verification must remain enabled.")
        return context

    def open_verified_websocket_socket(self) -> ssl.SSLSocket:
        """Open a direct, proxy-free TLS socket to the fixed local Gateway."""
        raw_socket: socket.socket | None = None
        try:
            context = self._verified_tls_context()
            raw_socket = socket.create_connection(("localhost", 5000), timeout=5)
            return context.wrap_socket(raw_socket, server_hostname="localhost")
        except Exception:
            if raw_socket is not None:
                try:
                    raw_socket.close()
                except Exception:
                    pass
            raise IBKRClientPortalError(
                "Local Client Portal Gateway is unavailable or TLS verification failed."
            ) from None

    @property
    def websocket_cookie(self) -> str:
        if self._session_token is None:
            raise IBKRAuthenticationRequired("Authenticate in Client Portal Gateway first.")
        return f"api={self._session_token}"

    def tickle(self) -> str:
        """Refresh and verify the manually authenticated brokerage session."""
        context = self._verified_tls_context()
        request = urllib.request.Request(
            _TICKLE_URL,
            data=b"",
            headers={"Accept": "application/json", "Content-Length": "0"},
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
                _RejectRedirectHandler(),
            )
            response = opener.open(request, timeout=5)
            with response:
                body = response.read(_MAX_TICKLE_BYTES + 1)
        except Exception:
            # Transport exceptions may contain request headers; never echo them.
            raise IBKRClientPortalError("Local Client Portal Gateway is unavailable or TLS verification failed.") from None
        if len(body) > _MAX_TICKLE_BYTES:
            raise IBKRClientPortalError("Client Portal Gateway response exceeded the size limit.")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise IBKRClientPortalError("Client Portal Gateway returned an invalid response.") from None
        if not isinstance(payload, dict):
            raise IBKRClientPortalError("Client Portal Gateway returned an invalid response.")
        token = payload.get("session")
        auth = payload.get("iserver")
        auth = auth.get("authStatus") if isinstance(auth, dict) else None
        if not isinstance(token, str) or not _SESSION_TOKEN.fullmatch(token):
            raise IBKRAuthenticationRequired("Client Portal Gateway has no usable session; sign in manually.")
        if not isinstance(auth, dict) or auth.get("authenticated") is not True:
            self._session_token = None
            raise IBKRAuthenticationRequired("Brokerage session is not authenticated; sign in manually.")
        self._session_token = token
        return token


class IBKRClientPortalReadOnlyStream:
    """Single-instrument, read-only stream with explicit keepalive/renewal.

    `create_app` intentionally does not start a socket. The caller must invoke
    it, then call `run_forever` on the returned websocket-client app. The
    `run_forever` maintains the session every ~60s and renews `smd` after
    9 minutes. It exits on disconnection; the caller must explicitly reconnect.
    """

    def __init__(
        self,
        *,
        conid: int,
        ca_file: str | Path,
        on_update: Callable[[IBKRMarketUpdate], None],
        on_status: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        # Reuse the strict parser's conid validation without making a request.
        build_subscription_message(conid)
        if not callable(on_update):
            raise ValueError("on_update callback is required")
        self.conid = conid
        self._ca_file = Path(ca_file).expanduser().resolve()
        if not self._ca_file.is_file():
            raise IBKRClientPortalError("A local trusted certificate bundle is required.")
        self._on_update = on_update
        self._on_status = on_status or (lambda _status: None)
        self._clock = clock
        self._wall_clock = wall_clock
        self._session = IBKRClientPortalSession(self._ca_file)
        self._last_tickle: float | None = None
        self._last_subscription: float | None = None
        self._app: Any | None = None
        self._active_token: str | None = None
        self._lock = RLock()
        self._opened = Event()

    def _refresh_session(self) -> str:
        token = self._session.tickle()
        if not isinstance(token, str) or not _SESSION_TOKEN.fullmatch(token):
            raise IBKRAuthenticationRequired("Brokerage session is not authenticated; sign in manually.")
        self._last_tickle = self._clock()
        return token

    def create_app(self) -> Any:
        """Create the WebSocket app; this performs one explicit local /tickle."""
        with self._lock:
            if self._app is not None:
                raise IBKRClientPortalError("A market-data connection is already active.")
        try:
            from websocket import WebSocketApp  # type: ignore[import-not-found]
        except ImportError:
            raise IBKRClientPortalError(
                "The optional websocket-client package is required for a live connection."
            ) from None

        token = self._refresh_session()
        prepared_socket = self._session.open_verified_websocket_socket()
        cookie = f"api={token}"
        kwargs = {
            "url": _WEBSOCKET_URL,
            "cookie": cookie,
            "socket": prepared_socket,
            "on_open": self._on_open,
            "on_message": self._on_message,
            "on_error": self._on_error,
            "on_close": self._on_close,
        }
        try:
            app = WebSocketApp(**kwargs)
        except Exception:
            try:
                prepared_socket.close()
            except Exception:
                pass
            raise IBKRClientPortalError("Unable to prepare the local market-data connection.") from None
        with self._lock:
            if self._app is not None:
                try:
                    prepared_socket.close()
                except Exception:
                    pass
                raise IBKRClientPortalError("A market-data connection is already active.")
            self._app = app
            self._active_token = token
        return app

    def run_forever(self, app: Any) -> None:
        """Run a previously created app with verified TLS and timed renewal."""
        if app is not self._app:
            raise IBKRClientPortalError("This WebSocket app does not belong to the stream instance.")
        stop = Event()
        worker = Thread(target=self._maintenance_loop, args=(app, stop), daemon=True)
        worker.start()
        try:
            app.run_forever(sslopt={
                "cert_reqs": ssl.CERT_REQUIRED,
                "ca_certs": str(self._ca_file),
                "check_hostname": True,
            })
        except Exception:
            self._on_status("CONNECTION_ERROR")
            raise IBKRClientPortalError("The verified local market-data connection failed.") from None
        finally:
            stop.set()
            worker.join(timeout=2)
            try:
                app.close()
            except Exception:
                pass
            with self._lock:
                if self._app is app:
                    self._app = None
                    self._active_token = None
            self._opened.clear()

    def _maintenance_loop(self, app: Any, stop: Event) -> None:
        if not self._opened.wait(timeout=30):
            return
        while not stop.wait(1):
            now = self._clock()
            with self._lock:
                last_tickle = self._last_tickle
                last_subscription = self._last_subscription
            if ((last_tickle is not None and now - last_tickle < 60)
                    and (last_subscription is not None and now - last_subscription < 9 * 60)):
                continue
            try:
                self.maintain(now=now)
            except IBKRClientPortalError:
                try:
                    app.close()
                except Exception:
                    pass
                return

    def maintain(self, *, now: float | None = None) -> None:
        """Keep the brokerage session alive and renew the stream before expiry."""
        current = self._clock() if now is None else now
        with self._lock:
            app = self._app
            last_tickle = self._last_tickle
            last_subscription = self._last_subscription
        if app is None or not self._opened.is_set():
            raise IBKRClientPortalError("Market-data stream is not connected.")
        if last_tickle is None or current - last_tickle >= 60:
            try:
                token = self._refresh_session()
            except IBKRAuthenticationRequired:
                self._on_status("AUTH_REQUIRED")
                try:
                    app.close()
                except Exception:
                    pass
                raise
            except IBKRClientPortalError:
                with self._lock:
                    if self._app is app:
                        self._app = None
                        self._active_token = None
                self._on_status("CONNECTION_ERROR")
                try:
                    app.close()
                except Exception:
                    pass
                raise
            if token != self._active_token:
                with self._lock:
                    if self._app is app:
                        self._app = None
                        self._active_token = None
                self._on_status("RECONNECT_REQUIRED")
                try:
                    app.close()
                except Exception:
                    pass
                raise IBKRClientPortalError("Session changed; create a new stream connection.")
            try:
                app.send("tic")
            except Exception:
                self._on_status("DISCONNECTED")
                raise IBKRClientPortalError("Market-data connection was lost.") from None
        if last_subscription is None or current - last_subscription >= 9 * 60:
            try:
                app.send(build_subscription_message(self.conid))
            except Exception:
                self._on_status("DISCONNECTED")
                raise IBKRClientPortalError("Market-data stream renewal failed.") from None
            with self._lock:
                self._last_subscription = current

    def disconnect(self) -> None:
        """Close only this read-only market-data stream."""
        with self._lock:
            app = self._app
            if app is None:
                return
            self._on_status("DISCONNECTING")
        try:
            app.send(build_unsubscription_message(self.conid))
        except Exception:
            pass
        try:
            app.close()
        except Exception:
            pass
        with self._lock:
            if self._app is app:
                self._app = None
                self._active_token = None
        self._opened.clear()
        self._on_status("DISCONNECTED")

    def _on_open(self, app: Any) -> None:
        try:
            app.send(build_subscription_message(self.conid))
        except Exception:
            self._on_status("DISCONNECTED")
            try:
                app.close()
            except Exception:
                pass
            return
        with self._lock:
            if self._app is not app:
                try:
                    app.close()
                except Exception:
                    pass
                return
            self._app = app
            self._last_subscription = self._clock()
            self._opened.set()
        self._on_status("CONNECTED_READ_ONLY")

    def _on_message(self, _app: Any, payload: str | bytes) -> None:
        try:
            if isinstance(payload, bytes):
                decoded = json.loads(payload)
            else:
                decoded = json.loads(payload)
            if not isinstance(decoded, Mapping) or decoded.get("topic") not in (None, f"smd+{self.conid}"):
                return
            if "conid" not in decoded:
                return
            update = parse_market_data_message(
                payload,
                expected_conid=self.conid,
                received_at=self._wall_clock(),
            )
        except (ValueError, TypeError):
            self._on_status("INVALID_MARKET_DATA")
            return
        self._on_update(update)

    def _on_error(self, _app: Any, _error: object) -> None:
        # Do not expose library exceptions which can include handshake headers.
        self._on_status("CONNECTION_ERROR")

    def _on_close(self, app: Any, *_args: object) -> None:
        with self._lock:
            if self._app is app:
                self._app = None
                self._active_token = None
                self._opened.clear()
                self._on_status("DISCONNECTED")


__all__ = [
    "IBKRAuthenticationRequired",
    "IBKRClientPortalError",
    "IBKRClientPortalReadOnlyStream",
    "IBKRClientPortalSession",
]
