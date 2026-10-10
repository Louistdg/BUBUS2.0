from __future__ import annotations

import hashlib
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from invest_ai.data.cryptocom_market_readonly import (
    CryptoComMarketDataError,
    CryptoComPublicMarketStream,
)


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
        for sent in self.sent:
            request = json.loads(sent)
            if request.get("method") == "subscribe":
                self.on_message(self, json.dumps({"id": request["id"], "method": "subscribe", "code": 0,
                    "result": {"channel": "ticker", "data": []}}))
        event = json.dumps({
            "id": -1, "method": "subscribe", "code": 0,
            "result": {
                "channel": "ticker", "instrument_name": "BTC_USDT",
                "data": [{"i": "BTC_USDT", "a": "62001.25", "b": "62000", "k": "62002",
                    "bs": "12.5", "ks": "10.5", "t": 1791570000123}],
            },
        })
        self.on_message(self, event)
        self.received.set()
        self.closed.wait(3)

    def close(self):
        self.closed.set()
        self.on_close(self, 1000, "closed")


class CryptoComPublicMarketStreamTests(unittest.TestCase):
    def test_public_stream_needs_no_key_and_exposes_real_ticker_provenance(self):
        apps = []

        def factory(url, **callbacks):
            app = FakeWebSocketApp(url, **callbacks)
            apps.append(app)
            return app

        stream = CryptoComPublicMarketStream(
            websocket_factory=factory,
            clock=lambda: datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc),
            sleeper=lambda _seconds: None,
        )
        initial = stream.connect("btc_usdt, ETH_USDT")
        app = apps[0]
        self.assertTrue(app.opened.wait(1))
        self.assertTrue(app.received.wait(1))
        self.assertEqual(app.url, "wss://stream.crypto.com/exchange/v1/market")
        subscribe = json.loads(app.sent[0])
        self.assertEqual(subscribe["method"], "subscribe")
        self.assertEqual(subscribe["params"]["channels"], ["ticker.BTC_USDT", "ticker.ETH_USDT"])
        self.assertEqual(app.ssl_options["cert_reqs"], 2)
        self.assertTrue(app.ssl_options["check_hostname"])
        self.assertIn(initial["connection_status"], {"CONNECTING", "SUBSCRIBING", "CONNECTED"})
        state = stream.state()
        self.assertEqual(state["provider"], "Crypto.com Exchange")
        self.assertEqual(state["symbols"], ["BTC_USDT", "ETH_USDT"])
        self.assertEqual(state["subscribed_symbols"], ["BTC_USDT", "ETH_USDT"])
        self.assertEqual(state["quotes"][0]["price"], "62001.25")
        self.assertEqual(state["quotes"][0]["last_trade_price"], "62001.25")
        self.assertEqual(state["quotes"][0]["best_bid"], "62000")
        self.assertEqual(state["quotes"][0]["best_ask"], "62002")
        self.assertEqual(state["quotes"][0]["best_bid_size"], "12.5")
        self.assertEqual(state["quotes"][0]["best_ask_size"], "10.5")
        self.assertEqual(state["quotes"][0]["quote_status"], "COMPLETE")
        self.assertEqual(state["quotes"][0]["provider_timestamp"], "2026-10-09T18:20:00.123Z")
        self.assertEqual(state["quotes"][0]["currency"], "USDT")
        self.assertEqual(state["quotes"][0]["payload_sha256"], hashlib.sha256(
            json.dumps({
                "id": -1, "method": "subscribe", "code": 0,
                "result": {
                    "channel": "ticker", "instrument_name": "BTC_USDT",
                    "data": [{"i": "BTC_USDT", "a": "62001.25", "b": "62000", "k": "62002",
                        "bs": "12.5", "ks": "10.5", "t": 1791570000123}],
                },
            }).encode()
        ).hexdigest())
        self.assertFalse(state["persistent"])
        self.assertFalse(state["usable_for_risk"])
        self.assertFalse(state["usable_for_execution"])
        self.assertFalse(state["orders_enabled"])

        app.on_message(app, json.dumps({"id": 10, "method": "public/heartbeat", "code": 0}))
        self.assertEqual(json.loads(app.sent[-1]), {"id": 10, "method": "public/respond-heartbeat"})
        stopped = stream.disconnect()
        self.assertEqual(stopped["connection_status"], "DISCONNECTED")
        self.assertEqual(stopped["quotes"], [])

    def test_optional_live_candlesticks_share_bounded_ticker_subscriptions(self):
        apps = []
        candles = []
        now = datetime.now(timezone.utc)

        def factory(url, **callbacks):
            app = FakeWebSocketApp(url, **callbacks); apps.append(app); return app

        stream = CryptoComPublicMarketStream(websocket_factory=factory, clock=lambda: now,
            sleeper=lambda _seconds: None)
        stream.configure_candlesticks("5m", lambda *event: candles.append(event))
        stream.connect("BTC_USDT")
        app = apps[0]
        self.assertTrue(app.opened.wait(1))
        request = json.loads(app.sent[0])
        self.assertEqual(request["params"]["channels"], [
            "ticker.BTC_USDT", "candlestick.5m.BTC_USDT"])
        row = {"o": "10", "h": "12", "l": "9", "c": "11", "v": "100", "t": 1791570000000}
        event = {"id": -1, "method": "subscribe", "code": 0, "result": {
            "channel": "candlestick", "instrument_name": "BTC_USDT",
            "subscription": "candlestick.5m.BTC_USDT", "interval": "M5", "data": [row]}}
        raw = json.dumps(event)
        app.on_message(app, raw)
        self.assertEqual(len(candles), 1)
        symbol, timeframe, candle, received_at, digest = candles[0]
        self.assertEqual((symbol, timeframe, candle), ("BTC_USDT", "5m", row))
        self.assertEqual(received_at, now.timestamp())
        self.assertEqual(digest, hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(stream.state()["candlestick_timeframe"], "5m")
        stream.disconnect()

    def test_live_candlestick_parser_rejects_other_interval_and_unrequested_pair(self):
        apps = []; candles = []; ready = threading.Event()
        class IdleSocket(FakeWebSocketApp):
            def run_forever(self, **kwargs):
                self.on_open(self); apps.append(self); ready.set(); self.closed.wait(2)
        stream = CryptoComPublicMarketStream(websocket_factory=lambda url, **kw: IdleSocket(url, **kw),
            sleeper=lambda _seconds: None)
        stream.configure_candlesticks("5m", lambda *event: candles.append(event))
        stream.connect("BTC_USDT"); self.assertTrue(ready.wait(1)); app = apps[0]
        for symbol, interval in (("BTC_USDT", "H1"), ("ETH_USDT", "M5")):
            app.on_message(app, json.dumps({"result": {"channel": "candlestick",
                "instrument_name": symbol, "subscription": f"candlestick.{interval}.{symbol}",
                "interval": interval, "data": [{"o": "1", "h": "2", "l": "1",
                    "c": "2", "v": "1", "t": 1791570000000}]}}))
        self.assertEqual(candles, [])
        stream.disconnect()

    def test_instrument_validation_rejects_bad_duplicates_and_unbounded_lists(self):
        for symbols in ("", "BTC_USDT,BTC_USDT", "../secret", "BAD SYMBOL",
                        ",".join(f"COIN{i}_USDT" for i in range(2001))):
            with self.subTest(symbols=symbols), self.assertRaises(CryptoComMarketDataError):
                CryptoComPublicMarketStream.validate_symbols(symbols)

    def test_many_instruments_are_subscribed_in_bounded_batches(self):
        apps = []
        def factory(url, **callbacks):
            app = FakeWebSocketApp(url, **callbacks); apps.append(app); return app
        symbols = ",".join(f"COIN{i}_USDT" for i in range(257))
        stream = CryptoComPublicMarketStream(websocket_factory=factory, sleeper=lambda _seconds: None)
        stream.connect(symbols)
        app = apps[0]
        self.assertTrue(app.opened.wait(1))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and stream.state()["connection_status"] != "CONNECTED":
            time.sleep(0.01)
        subscriptions = [json.loads(item) for item in app.sent if json.loads(item).get("method") == "subscribe"]
        self.assertEqual([len(item["params"]["channels"]) for item in subscriptions], [128, 128, 1])
        self.assertEqual(len(stream.state()["subscribed_symbols"]), 257)
        stream.disconnect()

    def test_more_than_connection_capacity_uses_parallel_public_connections(self):
        apps = []
        def factory(url, **callbacks):
            app = FakeWebSocketApp(url, **callbacks); apps.append(app); return app
        symbols = ",".join(f"COIN{i}_USDT" for i in range(513))
        stream = CryptoComPublicMarketStream(websocket_factory=factory, sleeper=lambda _seconds: None)
        stream.connect(symbols)
        self.assertEqual(len(apps), 2)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and stream.state()["connection_status"] != "CONNECTED":
            time.sleep(0.01)
        state = stream.state()
        self.assertEqual(state["connection_status"], "CONNECTED")
        self.assertEqual(len(state["subscribed_symbols"]), 513)
        self.assertEqual([len(app.sent) for app in apps], [3, 2])
        stream.disconnect()

    def test_all_spot_markets_with_candles_stay_below_session_subscription_limit(self):
        apps = []
        def factory(url, **callbacks):
            app = FakeWebSocketApp(url, **callbacks); apps.append(app); return app
        symbols = ",".join(f"COIN{i}_USDT" for i in range(568))
        stream = CryptoComPublicMarketStream(websocket_factory=factory, sleeper=lambda _seconds: None)
        stream.configure_candlesticks("5m", lambda *_event: None)
        stream.connect(symbols)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and stream.state()["connection_status"] != "CONNECTED":
            time.sleep(0.01)
        state = stream.state()
        self.assertEqual(state["connection_status"], "CONNECTED")
        self.assertEqual(len(state["subscribed_symbols"]), 568)
        self.assertEqual(len(apps), 3)
        for app in apps:
            requests = [json.loads(item) for item in app.sent]
            channel_count = sum(len(item["params"]["channels"]) for item in requests)
            self.assertLessEqual(channel_count, 384)
        stream.disconnect()

    def test_null_or_unrequested_last_trade_is_not_fabricated(self):
        callback = {}
        ready = threading.Event()

        class IdleSocket(FakeWebSocketApp):
            def run_forever(self, **kwargs):
                self.on_open(self)
                callback["app"] = self
                ready.set()
                self.closed.wait(2)

        stream = CryptoComPublicMarketStream(
            websocket_factory=lambda url, **kwargs: IdleSocket(url, **kwargs),
            sleeper=lambda _seconds: None,
        )
        stream.connect("BTC_USDT")
        self.assertTrue(ready.wait(1))
        app = callback["app"]
        app.on_message(app, json.dumps({"result": {"channel": "ticker", "data": [
            {"i": "BTC_USDT", "a": None, "t": 1791570000123},
            {"i": "ETH_USDT", "a": "100", "t": 1791570000123},
        ]}}))
        self.assertEqual(stream.state()["quotes"], [])
        stream.disconnect()

    def test_stream_marks_crossed_quotes_and_keeps_them_non_authoritative(self):
        callback = {}; ready = threading.Event()
        class IdleSocket(FakeWebSocketApp):
            def run_forever(self, **kwargs):
                self.on_open(self); callback["app"] = self; ready.set(); self.closed.wait(2)
        stream = CryptoComPublicMarketStream(websocket_factory=lambda url, **kw: IdleSocket(url, **kw), sleeper=lambda _: None)
        stream.connect("BTC_USDT"); self.assertTrue(ready.wait(1)); app = callback["app"]
        app.on_message(app, json.dumps({"result": {"channel": "ticker", "data": [
            {"i": "BTC_USDT", "a": "60000", "b": "60001", "k": "60000", "t": 1791570000123},
        ]}}))
        state = stream.state()
        self.assertEqual(state["quotes"][0]["quote_status"], "CROSSED")
        self.assertFalse(state["usable_for_risk"])
        self.assertFalse(state["orders_enabled"])
        stream.disconnect()

    def test_market_evidence_is_archived_reconstructed_and_freshness_checked(self):
        import tempfile
        from invest_ai.data.cryptocom_market_readonly import CryptoComMarketEvidenceStore, CryptoComMarketTick

        now = datetime.now(timezone.utc)
        tick = CryptoComMarketTick(
            symbol="BTC_USD", price=Decimal("62001.25"), best_bid=Decimal("62000"),
            best_ask=Decimal("62002"), best_bid_size=Decimal("12"), best_ask_size=Decimal("10"),
            quote_status="COMPLETE", provider_timestamp=now,
            received_at=now, currency="USD", exchange="Crypto.com Exchange", instrument_type="Exchange ticker",
            source="Crypto.com Exchange public ticker WebSocket", payload_sha256="a" * 64,
        )
        with tempfile.TemporaryDirectory() as root:
            store = CryptoComMarketEvidenceStore(root)
            record = store.record(tick, max_age_seconds=5)
            loaded = store.get(record.snapshot_id)
            self.assertEqual(loaded.snapshot, record.snapshot)
            self.assertEqual(loaded.snapshot.price, Decimal("62001"))  # bid/ask midpoint, not last trade
            self.assertEqual(loaded.snapshot.currency, "USD")
            fresh = store.assess(record.snapshot_id, as_of=datetime.now(timezone.utc), max_age_seconds=5)
            self.assertTrue(fresh.eligible)
            stale = store.assess(record.snapshot_id, as_of=now.replace(year=now.year + 1), max_age_seconds=5)
            self.assertFalse(stale.eligible)
            self.assertIn("STALE", stale.reasons)

    def test_market_evidence_uses_local_processing_time_when_exchange_clock_is_ahead_of_receive_time(self):
        import tempfile
        from invest_ai.data.cryptocom_market_readonly import CryptoComMarketEvidenceStore, CryptoComMarketTick

        received = datetime.now(timezone.utc)
        provider_time = received + timedelta(seconds=1)
        clock_values = iter((received, provider_time))
        waits = []
        tick = CryptoComMarketTick(
            symbol="BTC_EUR", price=Decimal("70000"), best_bid=Decimal("69999"),
            best_ask=Decimal("70001"), best_bid_size=Decimal("0.1"), best_ask_size=Decimal("0.1"),
            quote_status="COMPLETE", provider_timestamp=provider_time, received_at=received,
            currency="EUR", exchange="Crypto.com Exchange", instrument_type="Exchange ticker",
            source="Crypto.com Exchange public ticker WebSocket", payload_sha256="b" * 64,
        )
        with tempfile.TemporaryDirectory() as root:
            store = CryptoComMarketEvidenceStore(root, clock=lambda: next(clock_values), sleeper=waits.append)
            record = store.record(tick, max_age_seconds=5)
            self.assertEqual(record.snapshot.observed_at, provider_time)
            self.assertEqual(record.snapshot.received_at, received)
            self.assertEqual(record.snapshot.as_of, provider_time)
            self.assertEqual(waits, [1.0])
            self.assertTrue(store.assess(record.snapshot_id, as_of=provider_time,
                max_age_seconds=5).eligible)

    def test_market_evidence_rejects_provider_clock_skew_beyond_bounded_wait(self):
        import tempfile
        from pathlib import Path
        from invest_ai.data.cryptocom_market_readonly import CryptoComMarketEvidenceStore, CryptoComMarketTick

        now = datetime.now(timezone.utc)
        tick = CryptoComMarketTick("BTC_EUR", Decimal("70000"), Decimal("69999"), Decimal("70001"),
            "COMPLETE", now + timedelta(seconds=3), now, "EUR", "Crypto.com Exchange", "Spot", "ticker", "c" * 64)
        with tempfile.TemporaryDirectory() as root:
            store = CryptoComMarketEvidenceStore(root, clock=lambda: now, sleeper=lambda _: None)
            with self.assertRaises(CryptoComMarketDataError):
                store.record(tick)
            self.assertFalse((Path(root) / "data" / "raw").exists())

    def test_market_evidence_refuses_incomplete_or_crossed_quotes_before_archiving(self):
        import tempfile
        from pathlib import Path
        from invest_ai.data.cryptocom_market_readonly import CryptoComMarketEvidenceStore, CryptoComMarketTick

        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as root:
            store = CryptoComMarketEvidenceStore(root)
            for status, bid, ask in (("INCOMPLETE", None, None), ("CROSSED", Decimal("11"), Decimal("10"))):
                tick = CryptoComMarketTick("BTC_USD", Decimal("10"), bid, ask, status, now, now,
                    "USDT", "Crypto.com Exchange", "Spot", "ticker", "a" * 64)
                with self.subTest(status=status), self.assertRaises(CryptoComMarketDataError):
                    store.record(tick)
            self.assertFalse((Path(root) / "data" / "raw").exists())

    def test_market_evidence_rejects_derivatives_bad_provenance_and_nonspot_quote(self):
        import tempfile
        from pathlib import Path
        from invest_ai.data.cryptocom_market_readonly import CryptoComMarketEvidenceStore, CryptoComMarketTick

        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as root:
            store = CryptoComMarketEvidenceStore(root)
            invalid_ticks = (
                CryptoComMarketTick("BTCUSD-PERP", Decimal("10"), Decimal("9"), Decimal("11"), "COMPLETE",
                    now, now, "USD", "Crypto.com Exchange", "Perpetual", "ticker", "a" * 64),
                CryptoComMarketTick("BTC_USD", Decimal("10"), Decimal("9"), Decimal("11"), "COMPLETE",
                    now, now, "USD", "Other exchange", "Spot", "ticker", "a" * 64),
                CryptoComMarketTick("BTC_USD", Decimal("10"), Decimal("9"), Decimal("11"), "COMPLETE",
                    now, now, "USD", "Crypto.com Exchange", "Spot", "ticker", "invalid"),
            )
            for tick in invalid_ticks:
                with self.subTest(symbol=tick.symbol, source=tick.exchange), self.assertRaises(CryptoComMarketDataError):
                    store.record(tick)
            self.assertFalse((Path(root) / "data" / "raw").exists())


if __name__ == "__main__":
    unittest.main()
