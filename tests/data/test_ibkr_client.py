from __future__ import annotations

import json
import ssl
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from invest_ai.data.ibkr_client import (
    IBKRAuthenticationRequired,
    IBKRClientPortalError,
    IBKRClientPortalReadOnlyStream,
    IBKRClientPortalSession,
    _RejectRedirectHandler,
)
from invest_ai.data.ibkr_stream import IBKRMarketDataStatus, build_subscription_message


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.body


class _FakeOpener:
    def __init__(self, response: _Response, calls: list) -> None:
        self.response = response
        self.calls = calls

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        return self.response


class _FakeSocket:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.sent: list[str] = []
        self.closed = False
        self.sslopt = None
        self.run_error: Exception | None = None

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self) -> None:
        self.closed = True

    def run_forever(self, *, sslopt) -> None:
        self.sslopt = sslopt
        if self.run_error:
            raise self.run_error


class _FakeRawSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeTLSContext:
    verify_mode = ssl.CERT_REQUIRED
    check_hostname = True

    def __init__(self, prepared_socket) -> None:
        self.prepared_socket = prepared_socket
        self.server_names: list[str] = []

    def wrap_socket(self, raw_socket, *, server_hostname):
        self.raw_socket = raw_socket
        self.server_names.append(server_hostname)
        return self.prepared_socket


class IBKRClientPortalSessionTests(unittest.TestCase):
    def test_tickle_uses_fixed_local_post_and_requires_authenticated_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ca_file = Path(directory) / "ca.pem"
            ca_file.write_text("fixture only", encoding="utf-8")
            response = _Response(json.dumps({
                    "session": "safeToken_1",
                    "iserver": {"authStatus": {"authenticated": True}},
                }).encode())
            calls = []
            handlers = []
            context = ssl.create_default_context()

            def build_opener(*received_handlers):
                handlers.extend(received_handlers)
                return _FakeOpener(response, calls)

            session = IBKRClientPortalSession(ca_file)
            with patch("invest_ai.data.ibkr_client.ssl.create_default_context", return_value=context), \
                    patch("invest_ai.data.ibkr_client.urllib.request.build_opener",
                          side_effect=build_opener):
                self.assertEqual(session.tickle(), "safeToken_1")
            self.assertEqual(session.websocket_cookie, "api=safeToken_1")
            request, timeout = calls[0]
            self.assertEqual(request.full_url, "https://localhost:5000/v1/api/tickle")
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.data, b"")
            self.assertEqual(timeout, 5)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], urllib.request.HTTPSHandler)
            self.assertIsInstance(handlers[2], _RejectRedirectHandler)

    def test_tickle_rejects_unauthenticated_and_oversized_responses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ca_file = Path(directory) / "ca.pem"
            ca_file.write_text("fixture only", encoding="utf-8")
            unauth = IBKRClientPortalSession(ca_file)
            context = ssl.create_default_context()
            with patch("invest_ai.data.ibkr_client.ssl.create_default_context", return_value=context), \
                    patch("invest_ai.data.ibkr_client.urllib.request.build_opener", side_effect=lambda *_handlers:
                          _FakeOpener(_Response(
                              b'{"session":"token","iserver":{"authStatus":{"authenticated":false}}}'
                          ), [])), self.assertRaises(IBKRAuthenticationRequired):
                unauth.tickle()
            self.assertNotIn("token", repr(unauth))

            oversized = IBKRClientPortalSession(ca_file)
            context = ssl.create_default_context()
            with patch("invest_ai.data.ibkr_client.ssl.create_default_context", return_value=context), \
                    patch("invest_ai.data.ibkr_client.urllib.request.build_opener", side_effect=lambda *_handlers:
                          _FakeOpener(_Response(b" " * (32 * 1024 + 1)), [])), \
                    self.assertRaises(IBKRClientPortalError):
                oversized.tickle()

    def test_missing_certificate_fails_without_network(self) -> None:
        with self.assertRaises(IBKRClientPortalError):
            IBKRClientPortalSession(Path("missing-local-cert.pem"))


class IBKRClientPortalReadOnlyStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.ca_file = Path(self.temp.name) / "ca.pem"
        self.ca_file.write_text("fixture only", encoding="utf-8")
        self.app = None
        self.statuses: list[str] = []
        self.updates = []
        self.now = 1000.0

        def factory(**kwargs):
            self.app = _FakeSocket(**kwargs)
            return self.app
        self.factory = factory

        self.stream = IBKRClientPortalReadOnlyStream(
            conid=265598,
            ca_file=self.ca_file,
            on_update=self.updates.append,
            on_status=self.statuses.append,
            clock=lambda: self.now,
            wall_clock=lambda: datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
        )
        # Replace only the private transport boundary in tests; production has
        # no constructor option that can bypass `/tickle` authentication.
        self.stream._session.tickle = lambda: "safeToken_1"

    def create_app(self):
        self.prepared_socket = object()
        self.raw_socket = _FakeRawSocket()
        self.tls_context = _FakeTLSContext(self.prepared_socket)
        with patch.dict(sys.modules, {"websocket": SimpleNamespace(WebSocketApp=self.factory)}), \
                patch("invest_ai.data.ibkr_client.socket.create_connection", return_value=self.raw_socket) as connect, \
                patch("invest_ai.data.ibkr_client.ssl.create_default_context", return_value=self.tls_context):
            self.connect_mock = connect
            return self.stream.create_app()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_create_is_explicit_read_only_local_and_subscribes_on_open(self) -> None:
        app = self.create_app()
        self.assertEqual(app.kwargs["url"], "wss://localhost:5000/v1/api/ws")
        self.assertEqual(app.kwargs["cookie"], "api=safeToken_1")
        self.assertIs(app.kwargs["socket"], self.prepared_socket)
        self.connect_mock.assert_called_once_with(("localhost", 5000), timeout=5)
        self.assertEqual(self.tls_context.server_names, ["localhost"])
        self.assertIs(self.tls_context.raw_socket, self.raw_socket)
        self.assertFalse(self.raw_socket.closed)
        app.kwargs["on_open"](app)
        self.assertEqual(app.sent, [build_subscription_message(265598)])
        self.assertEqual(self.statuses, ["CONNECTED_READ_ONLY"])
        self.stream.run_forever(app)
        self.assertEqual(app.sslopt["cert_reqs"], ssl.CERT_REQUIRED)
        self.assertTrue(app.sslopt["check_hostname"])
        self.assertEqual(app.sslopt["ca_certs"], str(self.ca_file.resolve()))

    def test_stream_parses_only_requested_instrument(self) -> None:
        app = self.create_app()
        now_millis = int(datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        app.kwargs["on_message"](app, json.dumps({
            "topic": "smd+265598", "conid": 265598, "_updated": str(now_millis),
            "84": "100.10", "86": "100.11", "6509": "R",
        }))
        self.assertEqual(len(self.updates), 1)
        self.assertEqual(self.updates[0].status, IBKRMarketDataStatus.REALTIME)
        app.kwargs["on_message"](app, json.dumps({
            "topic": "smd+999", "conid": 999, "_updated": str(now_millis),
        }))
        self.assertEqual(len(self.updates), 1)

    def test_duplicate_connection_and_stale_socket_callbacks_are_rejected(self) -> None:
        app = self.create_app()
        with self.assertRaises(IBKRClientPortalError):
            self.stream.create_app()
        self.assertIs(self.stream._app, app)
        self.stream._on_close(object())
        self.assertIs(self.stream._app, app)
        self.assertEqual(self.statuses, [])

    def test_close_resets_session_and_allows_explicit_reconnect(self) -> None:
        first = self.create_app()
        first.kwargs["on_open"](first)
        first.kwargs["on_close"](first)
        self.assertIsNone(self.stream._app)
        self.assertIsNone(self.stream._active_token)
        self.assertEqual(self.statuses[-1], "DISCONNECTED")
        second = self.create_app()
        self.assertIs(self.stream._app, second)
        second.kwargs["on_open"](second)
        self.assertEqual(self.statuses[-1], "CONNECTED_READ_ONLY")

    def test_session_rotation_closes_stream_and_resets_token_ownership(self) -> None:
        app = self.create_app()
        app.kwargs["on_open"](app)
        self.stream._session.tickle = lambda: "safeToken_2"
        self.now += 60
        with self.assertRaises(IBKRClientPortalError):
            self.stream.maintain(now=self.now)
        self.assertIsNone(self.stream._app)
        self.assertIsNone(self.stream._active_token)
        self.assertTrue(app.closed)
        self.assertIn("RECONNECT_REQUIRED", self.statuses)

    def test_keepalive_and_stream_renewal_are_explicit_and_bounded(self) -> None:
        app = self.create_app()
        app.kwargs["on_open"](app)
        self.now += 9 * 60
        self.stream.maintain(now=self.now)
        self.assertEqual(app.sent[0], build_subscription_message(265598))
        self.assertEqual(app.sent[1], "tic")
        self.assertEqual(app.sent[2], build_subscription_message(265598))

    def test_maintenance_requires_an_open_socket(self) -> None:
        self.create_app()
        with self.assertRaises(IBKRClientPortalError):
            self.stream.maintain(now=self.now)

    def test_socket_error_text_is_not_exposed(self) -> None:
        app = self.create_app()
        app.kwargs["on_error"](app, "Cookie: api=secret")
        self.assertEqual(self.statuses, ["CONNECTION_ERROR"])
        self.assertNotIn("secret", " ".join(self.statuses))

    def test_runner_suppresses_sensitive_library_exception_text(self) -> None:
        app = self.create_app()
        app.kwargs["on_open"](app)
        app.run_error = RuntimeError("Cookie: api=secret")
        with self.assertRaises(IBKRClientPortalError) as captured:
            self.stream.run_forever(app)
        self.assertNotIn("secret", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
