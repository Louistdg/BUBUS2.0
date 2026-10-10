from __future__ import annotations

import hashlib
import json
import threading
import unittest
from datetime import datetime, timezone

from invest_ai.data.twelvedata_readonly import TwelveDataError, TwelveDataReadOnlyQuoteStream


class FakeWebSocketApp:
    def __init__(self, url, *, on_open, on_message, on_error, on_close):
        self.url = url
        self.on_open = on_open
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self.sent = []
        self.ssl_options = None
        self.closed = threading.Event()
        self.opened = threading.Event()
        self.received = threading.Event()

    def send(self, payload):
        self.sent.append(payload)

    def run_forever(self, **kwargs):
        self.ssl_options = kwargs.get("sslopt")
        self.on_open(self)
        self.opened.set()
        message = json.dumps({
            "event": "price", "symbol": "BTC/USD", "price": "61234.50",
            "timestamp": 1791570000, "currency": "USD", "exchange": "Crypto.com",
            "type": "Digital Currency",
        })
        self.on_message(self, message)
        self.received.set()
        self.closed.wait(timeout=3)

    def close(self):
        self.closed.set()
        self.on_close(self, 1000, "closed")


class TwelveDataReadOnlyQuoteStreamTests(unittest.TestCase):
    def test_explicit_connection_subscribes_to_user_symbols_and_exposes_provenance_only(self):
        apps = []

        def factory(url, **callbacks):
            app = FakeWebSocketApp(url, **callbacks)
            apps.append(app)
            return app

        stream = TwelveDataReadOnlyQuoteStream(
            websocket_factory=factory,
            clock=lambda: datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc),
        )
        stream.connect("api-key-private", "BTC/USD, EUR/USD")
        app = apps[0]
        self.assertTrue(app.opened.wait(1))
        self.assertTrue(app.received.wait(1))
        self.assertEqual(app.url, "wss://ws.twelvedata.com/v1/quotes/price?apikey=api-key-private")
        self.assertIn('"symbols":"BTC/USD,EUR/USD"', app.sent[0])
        self.assertEqual(app.ssl_options["cert_reqs"], 2)
        self.assertTrue(app.ssl_options["check_hostname"])
        state = stream.state()
        self.assertEqual(state["connection_status"], "CONNECTED")
        self.assertEqual(state["symbols"], ["BTC/USD", "EUR/USD"])
        self.assertEqual(len(state["quotes"]), 1)
        quote = state["quotes"][0]
        self.assertEqual(quote["symbol"], "BTC/USD")
        self.assertEqual(quote["price"], "61234.50")
        self.assertEqual(quote["currency"], "USD")
        self.assertEqual(quote["exchange"], "Crypto.com")
        self.assertEqual(quote["payload_sha256"], hashlib.sha256(json.dumps({
            "event": "price", "symbol": "BTC/USD", "price": "61234.50",
            "timestamp": 1791570000, "currency": "USD", "exchange": "Crypto.com",
            "type": "Digital Currency",
        }).encode()).hexdigest())
        self.assertFalse(state["persistent"])
        self.assertFalse(state["usable_for_risk"])
        self.assertFalse(state["usable_for_execution"])
        self.assertNotIn("api-key-private", json.dumps(state))
        self.assertNotIn("wss://", json.dumps(state))
        disconnected = stream.disconnect()
        self.assertEqual(disconnected["connection_status"], "DISCONNECTED")
        self.assertEqual(disconnected["quotes"], [])
        self.assertEqual(disconnected["symbols"], [])

    def test_symbol_validation_rejects_duplicates_invalid_characters_and_unbounded_lists(self):
        for symbols in ("", "AAPL,AAPL", "../secret", "AAPL, " + ",".join(f"S{i}" for i in range(8))):
            with self.subTest(symbols=symbols), self.assertRaises(TwelveDataError):
                TwelveDataReadOnlyQuoteStream.validate_symbols(symbols)

    def test_invalid_or_unrequested_price_events_are_ignored(self):
        callbacks = {}
        ready = threading.Event()

        class IdleSocket(FakeWebSocketApp):
            def run_forever(self, **kwargs):
                self.ssl_options = kwargs.get("sslopt")
                self.on_open(self)
                callbacks["app"] = self
                ready.set()

        stream = TwelveDataReadOnlyQuoteStream(
            websocket_factory=lambda url, **kwargs: IdleSocket(url, **kwargs),
        )
        stream.connect("api-key-private", "AAPL")
        self.assertTrue(ready.wait(1))
        app = callbacks["app"]
        app.on_message(app, json.dumps({"event": "price", "symbol": "MSFT", "price": "1", "timestamp": 1791570000}))
        app.on_message(app, json.dumps({"event": "price", "symbol": "AAPL", "price": "NaN", "timestamp": 1791570000}))
        app.on_message(app, json.dumps({"event": "price", "symbol": "AAPL", "price": "100", "timestamp": "1791570000"}))
        self.assertEqual(stream.state()["quotes"], [])
        stream.disconnect()

    def test_invalid_credentials_rejected_before_creating_socket(self):
        calls = []
        stream = TwelveDataReadOnlyQuoteStream(websocket_factory=lambda *_args, **_kwargs: calls.append(True))
        with self.assertRaises(TwelveDataError):
            stream.connect("short", "AAPL")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
