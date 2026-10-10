from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bubus.server import make_server, start_local_public_market_tracking
from bubus.store import DeskStore
from tests.assistant.test_market_dashboard import FakeCryptoOverview, FakeCoinGeckoOverview
from invest_ai.portfolio.cryptocom_readonly import (
    CryptoComFeeSchedule, CryptoComTransaction, CryptoComTransactionPage,
    CryptoComTransactionPageSet, CryptoComExecutedTrade, CryptoComTradePage, CryptoComTradePageSet,
)


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(0, data_dir=self.temp.name,
            cryptocom_market_overview=FakeCryptoOverview(), coingecko_market_overview=FakeCoinGeckoOverview())
        self.server.cryptocom_instrument_catalog = type("Catalog", (), {
            "get_spot_catalog": lambda _self: {"BTC_USDT": {
                "base_currency": "BTC", "quote_currency": "USDT",
                "price_tick_size": "0.01", "quantity_tick_size": "0.00001", "tradable": "true"}}})()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        self.temp.cleanup()

    def test_desk_uses_branded_assets_and_does_not_claim_trading_is_ready(self):
        with urlopen(self.url) as response:
            html = response.read().decode()
        self.assertIn("BUBUS2.0", html)
        self.assertIn("desk.js", html)
        self.assertIn("portfolio-candidate", html)
        self.assertIn("Calculer la valeur Spot candidate", html)
        self.assertIn("Exécutions confirmées Crypto.com", html)
        self.assertIn('id="trade-status"', html)
        self.assertIn('id="observer-key-form"', html)
        self.assertIn('id="observer-start"', html)
        self.assertIn("risk-limits-form", html)
        self.assertEqual(html.count('data-theme-choice='), 4)
        self.assertIn('data-theme-choice="nuit" aria-pressed="true"', html)
        self.assertIn('aria-label="Thème de couleurs"', html)
        with urlopen(self.url + "/assets/desk.css") as response:
            css = response.read().decode()
        self.assertIn(':root[data-theme="nuit"]', css)
        self.assertIn("theme-swatch-ocean", css)
        self.assertIn(':root[data-theme="foret"]', css)
        self.assertIn(':root[data-theme="ocean"]', css)
        self.assertIn("--chart-line", css)
        with urlopen(self.url + "/assets/desk.js") as response:
            script = response.read().decode()
        self.assertIn("localStorage.getItem('bubus.theme')", script)
        self.assertIn("localStorage.getItem('bubus.theme') || 'nuit'", script)
        self.assertIn("if (!document.querySelector('.cloud-preview-banner'))", script)
        self.assertIn("/api/cryptocom/portfolio/refresh", script)
        self.assertIn("use_observer_credentials", script)
        self.assertIn("let legacyCredentialsPersisted = null", script)
        self.assertIn("observerCredentialsPersisted = null", script)
        self.assertIn("function renderSavedCredentialControls()", script)
        self.assertIn("legacyCredentialsPersisted !== null && observerCredentialsPersisted !== null", script)
        self.assertIn("selectedHistoryCredentials()", script)
        self.assertIn('id="saved-refresh" class="button button-white" disabled', html)
        self.assertIn('id="history-last-day" class="button button-white" type="button" disabled', html)
        self.assertIn('id="history-load" class="button button-green" type="button" disabled', html)
        self.assertIn("/api/cryptocom/portfolio-observer/start", script)
        self.assertIn("/api/cryptocom/portfolio/trades", script)
        self.assertIn("/api/cryptocom/portfolio/funding-history", script)
        self.assertIn("OPEN_ORDER_RESERVATIONS_MISMATCH", script)
        self.assertIn("open_orders_bound_to_latest_valuation", script)
        self.assertIn("Dépôts et retraits Crypto.com", script)
        self.assertIn("aucune lecture privée avant ton clic sur « Actualiser le compte »", script)
        self.assertIn("age <= 30", script)
        self.assertIn("Relevé périmé pour cette valorisation", script)
        saved_portfolio_loader = script.split("async function loadSavedPortfolio() {", 1)[1].split(
            "  $('live-signal-run').addEventListener", 1)[0]
        self.assertIn("api('/api/desk/portfolio')", saved_portfolio_loader)
        self.assertNotIn("use_saved_credentials", saved_portfolio_loader)
        self.assertNotIn("/api/cryptocom/portfolio/refresh", saved_portfolio_loader)
        self.assertEqual(script.count("$('live-signal-run').addEventListener"), 1)
        self.assertIn("/api/desk/live-signal?${params}", script)
        self.assertNotIn("research-run", html)
        self.assertNotIn("Réservations Risk — simulation", script)
        self.assertNotIn("loadOfflineRiskReservations", script)
        self.assertNotIn("loadOfflineOrderJournal", script)
        self.assertIn("Scanner continu des marchés", html)
        self.assertIn('id="live-scan-status"', html)
        self.assertIn("scan.fresh_signals_count", script)
        self.assertIn("const freshSignalRows = signalRows.filter(isFresh)", script)
        self.assertIn("freshnessWindow = Number.isFinite(scan.freshness_window_seconds)", script)
        self.assertIn("bougies ${result.candlestick_timeframe}", script)
        self.assertIn('function renderPortfolioValuations(items)', script)
        self.assertIn("if (!Array.isArray(items))", script)
        self.assertIn('valuationHistoryRows.replaceChildren()', script)
        self.assertIn('aucune utilisée par Risk ou les ordres', script)
        with urlopen(self.url + "/api/desk/state") as response:
            state = json.load(response)
        self.assertEqual(state["product"], "BUBUS2.0")
        self.assertFalse(state["trading"]["orders_enabled"])
        self.assertFalse(state["trading"]["armed"])

    def test_desk_configures_public_five_minute_candles_for_live_scanner(self):
        stream = self.server.cryptocom_market_stream
        self.assertEqual(stream._candlestick_timeframe, "5m")
        self.assertIs(stream._candlestick_listener.__self__, self.server.candles)
        self.assertIs(stream._candlestick_listener.__func__, self.server.candles.update_live_candle.__func__)
        self.assertIs(self.server.cryptocom_spot_order_adapter._order_journal,
                      self.server.live_order_journal)

    def test_portfolio_observer_is_explicit_and_uses_its_separate_can_read_vault(self):
        self.assertNotEqual(self.server.cryptocom_observer_credential_store._target,
                            self.server.cryptocom_credential_store._target)
        class MemoryCredentialStore:
            available = True

            def __init__(self):
                self.credentials = None

            def store(self, api_key, api_secret):
                self.credentials = (api_key, api_secret)

            def load(self):
                if self.credentials is None:
                    return None
                from invest_ai.portfolio.cryptocom_credential_store import CryptoComCredentials
                return CryptoComCredentials(*self.credentials)

            def has_credentials(self):
                return self.credentials is not None

        class ReadOnlyClient:
            def __init__(self):
                self.called = threading.Event()
                self.credentials = None

            def read_portfolio(self, api_key, api_secret):
                self.credentials = (api_key, api_secret)
                self.called.set()
                return SimpleNamespace(
                    provider="Crypto.com Exchange", source="fixture", fetched_at=datetime.now(timezone.utc),
                    authority_status="BROKER_READ_ONLY_OBSERVATION_ONLY", persistent=False,
                    usable_for_risk=False, usable_for_execution=False, payload_sha256="a" * 64,
                    positions_payload_sha256="b" * 64, open_orders_payload_sha256="c" * 64,
                    balances=(), open_orders=(), wallet_balances=(), positions=())

        credentials = MemoryCredentialStore()
        client = ReadOnlyClient()
        class ExistingTradeCredentialStore:
            available = True
            def load(self):
                from invest_ai.portfolio.cryptocom_credential_store import CryptoComCredentials
                return CryptoComCredentials("existing-trade-key", "existing-secret")

        self.server.cryptocom_observer_credential_store = credentials
        self.server.cryptocom_credential_store = ExistingTradeCredentialStore()
        self.server.cryptocom_portfolio_client = client

        with urlopen(self.url + "/api/desk/portfolio-observer") as response:
            state = json.load(response)
        self.assertEqual(state["observer"]["status"], "STOPPED")
        self.assertFalse(state["observer"]["active"])
        self.assertFalse(client.called.is_set())

        headers = {"Content-Type": "application/json", "Origin": self.url}
        reused_key_body = json.dumps({"api_key": "existing-trade-key", "api_secret": "other-secret",
                                      "confirm_read_only": True}).encode()
        with self.assertRaises(HTTPError) as refused_existing_key:
            urlopen(Request(self.url + "/api/cryptocom/portfolio-observer/credentials",
                            data=reused_key_body, headers=headers))
        self.assertEqual(refused_existing_key.exception.code, 409)
        refused_existing_key.exception.close()

        save_body = json.dumps({"api_key": "dedicated-read-key", "api_secret": "read-secret",
                                "confirm_read_only": True}).encode()
        with urlopen(Request(self.url + "/api/cryptocom/portfolio-observer/credentials",
                             data=save_body, headers=headers)) as response:
            self.assertTrue(json.load(response)["credentials_persisted"])
        self.assertEqual(credentials.credentials, ("dedicated-read-key", "read-secret"))

        start_body = json.dumps({"confirm_read_only": True}).encode()
        with urlopen(Request(self.url + "/api/cryptocom/portfolio-observer/start",
                             data=start_body, headers=headers)) as response:
            self.assertEqual(response.status, 202)
            start_result = json.load(response)
        self.assertFalse(start_result["observer"]["orders_enabled"])
        self.assertFalse(start_result["observer"]["usable_for_risk"])
        self.assertTrue(client.called.wait(1))
        self.assertEqual(client.credentials, ("dedicated-read-key", "read-secret"))
        deadline = time.monotonic() + 1
        while (not self.server.cryptocom_portfolio_observer.state()["last_success_at"]
               and time.monotonic() < deadline):
            time.sleep(0.005)
        self.assertIsNotNone(self.server.desk_store.latest_cryptocom_portfolio())

        with self.assertRaises(HTTPError) as refused_replace:
            urlopen(Request(self.url + "/api/cryptocom/portfolio-observer/credentials",
                data=save_body, headers=headers))
        self.assertEqual(refused_replace.exception.code, 409)
        refused_replace.exception.close()

        stop_body = json.dumps({"stop": True}).encode()
        with urlopen(Request(self.url + "/api/cryptocom/portfolio-observer/stop",
                             data=stop_body, headers=headers)) as response:
            self.assertTrue(json.load(response)["stopped"])
        state = self.server.cryptocom_portfolio_observer.state()
        self.assertFalse(state["active"])
        self.assertFalse(state["orders_enabled"])

    def test_portfolio_observer_refuses_start_without_explicit_read_only_confirmation(self):
        headers = {"Content-Type": "application/json", "Origin": self.url}
        body = json.dumps({}).encode()
        with self.assertRaises(HTTPError) as refused:
            urlopen(Request(self.url + "/api/cryptocom/portfolio-observer/start",
                            data=body, headers=headers))
        self.assertEqual(refused.exception.code, 400)
        refused.exception.close()
        self.assertEqual(self.server.cryptocom_portfolio_observer.state()["status"], "STOPPED")

    def test_portfolio_observer_refuses_to_start_without_its_separate_saved_key(self):
        class EmptyCredentialStore:
            available = True
            def load(self): return None

        self.server.cryptocom_observer_credential_store = EmptyCredentialStore()
        headers = {"Content-Type": "application/json", "Origin": self.url}
        body = json.dumps({"confirm_read_only": True}).encode()
        with self.assertRaises(HTTPError) as refused:
            urlopen(Request(self.url + "/api/cryptocom/portfolio-observer/start",
                            data=body, headers=headers))
        self.assertEqual(refused.exception.code, 409)
        refused.exception.close()
        self.assertEqual(self.server.cryptocom_portfolio_observer.state()["status"], "STOPPED")

    def test_watchlist_requires_same_origin_and_survives_store_restart(self):
        payload = json.dumps({"symbol": "BTC_USDT", "enabled": True}).encode()
        headers = {"Content-Type": "application/json"}
        with self.assertRaises(HTTPError) as refused:
            urlopen(Request(self.url + "/api/desk/watchlist", data=payload, headers=headers))
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()
        headers["Origin"] = self.url
        for _ in range(2):
            with urlopen(Request(self.url + "/api/desk/watchlist", data=payload, headers=headers)) as response:
                self.assertEqual(json.load(response)["watchlist"], ["BTC_USDT"])
        store = DeskStore(Path(self.temp.name) / "desk.sqlite3")
        self.assertEqual(store.watchlist(), ["BTC_USDT"])
        self.assertEqual(len(store.activity()), 1)

    def test_trade_batch_schema_migration_preserves_existing_observations(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        provider_digest = "a" * 64
        encoded = json.dumps({"items": [], "provider_payload_sha256": provider_digest},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        stored_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE cryptocom_trade_snapshots(id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, "
                "start_time_ns INTEGER NOT NULL, end_time_ns INTEGER NOT NULL, item_count INTEGER NOT NULL, "
                "payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL)")
            db.execute("INSERT INTO cryptocom_trade_snapshots VALUES(7,?,?,?,?,?,?)",
                ("2026-10-10T10:00:00+00:00", 1, 100, 0, encoded, stored_digest))
            db.commit()
        finally:
            db.close()
        store = DeskStore(path)
        db = sqlite3.connect(path)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(cryptocom_trade_snapshots)")}
        finally:
            db.close()
        self.assertIn("batch_id", columns)
        saved = store.cryptocom_trade_history(limit=10)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["snapshot_id"], 7)
        self.assertEqual(saved[0]["items"], [])

    def test_portfolio_valuation_candidates_persist_with_integrity_and_no_risk_authority(self):
        payload = {"snapshot_id": "cryptocom-spot-candidate:" + "a" * 64,
            "as_of": "2026-10-10T12:00:00+00:00", "base_currency": "EUR",
            "total_equity": "125.50", "cash_value": "100", "spot_assets_value": "25.50",
            "held_asset_values": [{"asset": "BTC", "value": "25.50"},
                                  {"asset": "EUR", "value": "100"}], "order_reservations": [],
            "market_snapshot_ids": ["market-snapshot-1"], "source_payload_sha256": ["b" * 64],
            "authority_status": "BROKER_SPOT_PORTFOLIO_CANDIDATE_ONLY", "usable_for_risk": False,
            "usable_for_execution": False, "orders_enabled": False}
        saved = self.server.desk_store.record_cryptocom_valuation_candidate(payload)
        self.assertTrue(saved["persistent"])
        self.assertFalse(saved["usable_for_risk"])
        self.assertFalse(saved["orders_enabled"])
        history = self.server.desk_store.cryptocom_valuation_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total_equity"], "125.50")
        self.assertFalse(history[0]["usable_for_execution"])
        with urlopen(self.url + "/api/desk/portfolio-valuations") as response:
            persisted = json.load(response)["valuations"]
        self.assertEqual(len(persisted), 1)
        self.assertFalse(persisted[0]["orders_enabled"])
        invalid = dict(payload, total_equity="126")
        with self.assertRaises(ValueError):
            self.server.desk_store.record_cryptocom_valuation_candidate(invalid)
        self.assertEqual(len(self.server.desk_store.cryptocom_valuation_history()), 1)
        db = sqlite3.connect(Path(self.temp.name) / "desk.sqlite3")
        try:
            db.execute("UPDATE cryptocom_portfolio_valuation_candidates SET payload_json='{}'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(ValueError):
            self.server.desk_store.cryptocom_valuation_history()
        with self.assertRaises(HTTPError) as refused:
            urlopen(self.url + "/api/desk/portfolio-valuations")
        self.assertEqual(refused.exception.code, 503)
        refused.exception.close()

        # A locally rehashed but inconsistent row must also fail closed.
        contradictory = dict(payload, held_asset_values=[{"asset": "BTC", "value": "25.50"}])
        encoded = json.dumps(contradictory, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        db = sqlite3.connect(Path(self.temp.name) / "desk.sqlite3")
        try:
            db.execute("UPDATE cryptocom_portfolio_valuation_candidates SET payload_json=?, payload_sha256=?",
                       (encoded, digest))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(ValueError):
            self.server.desk_store.cryptocom_valuation_history()
        with self.assertRaises(HTTPError) as refused:
            urlopen(self.url + "/api/desk/portfolio-valuations")
        self.assertEqual(refused.exception.code, 503)
        refused.exception.close()

    def test_portfolio_valuation_rejects_invalid_values_before_storage(self):
        store = self.server.desk_store
        payload = {"snapshot_id": "cryptocom-spot-candidate:" + "a" * 64,
                   "as_of": "2026-10-10T12:00:00+00:00", "base_currency": "EUR",
                   "total_equity": "100", "cash_value": "100", "spot_assets_value": "0",
                   "held_asset_values": [{"asset": "EUR", "value": "100"}],
                   "order_reservations": [], "market_snapshot_ids": [],
                   "source_payload_sha256": ["b" * 64],
                   "authority_status": "BROKER_SPOT_PORTFOLIO_CANDIDATE_ONLY",
                   "usable_for_risk": False, "usable_for_execution": False, "orders_enabled": False}
        variants = [
            dict(payload, total_equity="NaN"),
            dict(payload, as_of="2026-10-10T12:00:00"),
            dict(payload, usable_for_risk=True),
            dict(payload, held_asset_values=[{"asset": "EUR", "value": "99"}]),
            dict(payload, held_asset_values=[{"asset": "eur", "value": "100"}]),
            dict(payload, source_payload_sha256=[]),
            dict(payload, spot_assets_value="1"),
        ]
        for invalid in variants:
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    store.record_cryptocom_valuation_candidate(invalid)
        self.assertEqual(store.cryptocom_valuation_history(), [])
        store.record_cryptocom_valuation_candidate(payload)
        self.assertEqual(len(DeskStore(Path(self.temp.name) / "desk.sqlite3").cryptocom_valuation_history()), 1)

    def test_observation_history_limits_can_retrieve_a_full_utc_day(self):
        store = self.server.desk_store
        self.assertEqual(store.cryptocom_valuation_history(limit=2500), [])
        self.assertEqual(store.cryptocom_portfolio_history(limit=2500), [])

    def test_historical_research_is_chronological_and_never_execution_authority(self):
        from bubus.research import compare_candles
        candles = []
        for i in range(120):
            close = Decimal("100") + Decimal(i) / 10
            candles.append({"timestamp": (i + 1) * 3600000, "o": str(close), "h": str(close + 1),
                            "l": str(close - 1), "c": str(close), "v": "1000"})
        result = compare_candles(candles, commission_bps=10, slippage_bps=5, test_percent=30)
        self.assertEqual([row["id"] for row in result["results"]],
                         ["do_nothing", "buy_hold", "sma_cross", "breakout"])
        self.assertEqual(len(result["results"][0]["folds"]), 3)
        self.assertFalse(result["orders_enabled"])
        self.assertFalse(result["execution_allowed"])
        self.assertIn("Normalized capital index", result["notice"])
        daily = compare_candles(candles, commission_bps=10, slippage_bps=5, test_percent=30, timeframe="1D")
        daily_buy_hold = next(row for row in daily["results"] if row["id"] == "buy_hold")
        hourly_buy_hold = next(row for row in result["results"] if row["id"] == "buy_hold")
        self.assertIsNotNone(daily_buy_hold["annualized_volatility_percent"])
        self.assertLess(Decimal(daily_buy_hold["annualized_volatility_percent"]),
                        Decimal(hourly_buy_hold["annualized_volatility_percent"]))
        self.assertEqual(result["timeframe"], "1h")

    def test_historical_research_rejects_bad_costs_and_non_chronological_bars(self):
        from bubus.research import ResearchError, compare_candles
        candles = [{"timestamp": i, "o": "10", "h": "11", "l": "9", "c": "10", "v": "5"}
                   for i in range(1, 10)]
        with self.assertRaises(ResearchError):
            compare_candles(candles, commission_bps=1001)
        candles[5]["timestamp"] = candles[4]["timestamp"]
        with self.assertRaises(ResearchError):
            compare_candles(candles)

    def test_historical_research_route_is_local_and_same_origin_protected(self):
        class Candles:
            def get(self, symbol, timeframe):
                self.request = (symbol, timeframe)
                return {"candles": [{"timestamp": (i + 1) * 60000, "o": "10", "h": "11",
                    "l": "9", "c": str(10 + i / 100), "v": "100"} for i in range(60)]}
        self.server.candles = Candles()
        with urlopen(Request(self.url + "/api/desk/research?symbol=BTC_USDT")) as response:
            same_host = json.load(response)
        self.assertEqual(same_host["provider"], "Crypto.com Exchange")
        request = Request(self.url + "/api/desk/research?symbol=BTC_USDT&timeframe=1h",
                          headers={"Origin": self.url})
        with urlopen(request) as response:
            result = json.load(response)
        self.assertEqual(self.server.candles.request, ("BTC_USDT", "1h"))
        self.assertFalse(result["execution_allowed"])

    def test_live_signal_route_uses_current_evidence_and_returns_no_order_authority(self):
        from dataclasses import replace
        from tests.bubus_v2.test_live_signal import BASE, market_inputs

        payload, tick, _rules = market_inputs()
        now = datetime.now(timezone.utc)
        shift_ms = int((now - timedelta(seconds=2) - BASE).total_seconds() * 1000)
        payload = dict(payload, received_at=now.timestamp(), candles=[
            dict(row, timestamp=row["timestamp"] + shift_ms) for row in payload["candles"]])
        tick = replace(tick, provider_timestamp=now, received_at=now)

        class Stream:
            def state(self):
                return {"connection_status": "CONNECTED"}
            def get_tick(self, symbol):
                return tick if symbol == "BTC_USDT" else None
            def disconnect(self):
                return None

        class Candles:
            def get(self, symbol, timeframe):
                return payload

        self.server.cryptocom_market_stream = Stream()
        self.server.candles = Candles()
        with urlopen(self.url + "/api/desk/live-signal?symbol=BTC_USDT&timeframe=5m") as response:
            result = json.load(response)
        self.assertEqual(result["status"], "SIGNAL_OBSERVED", result)
        self.assertEqual(result["direction"], "UP_BREAKOUT")
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["orders_enabled"])
        self.assertNotIn("quantity", result)
        self.assertNotIn("order", result)

    def test_live_signal_route_fails_closed_without_stream_or_for_bad_query(self):
        request = self.url + "/api/desk/live-signal?symbol=BTC_USDT&timeframe=5m"
        with self.assertRaises(HTTPError) as disconnected:
            urlopen(request)
        self.assertEqual(disconnected.exception.code, 409)
        disconnected.exception.close()
        with self.assertRaises(HTTPError) as invalid:
            urlopen(self.url + "/api/desk/live-signal?symbol=BTC_USDT&timeframe=1w")
        self.assertEqual(invalid.exception.code, 400)
        invalid.exception.close()

    def test_live_signal_scanner_state_is_local_read_only_and_has_no_execution_authority(self):
        with urlopen(self.url + "/api/desk/live-signals") as response:
            state = json.load(response)
        self.assertEqual(state["status"], "STOPPED")
        self.assertFalse(state["orders_enabled"])
        self.assertFalse(state["execution_allowed"])
        request = Request(self.url + "/api/desk/live-signals",
                          headers={"Origin": "https://foreign.example"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(request)
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()

    def test_local_application_start_tracks_all_public_spot_pairs_without_credentials(self):
        class Stream:
            symbols = None
            def connect(self, symbols):
                self.symbols = symbols.split(",")
                return {"connection_status": "CONNECTING", "symbols": self.symbols,
                        "orders_enabled": False}
            def state(self): return {"connection_status": "DISCONNECTED", "symbols": []}
            def disconnect(self): return {"connection_status": "DISCONNECTED"}
        class Scanner:
            started = None
            def start(self, symbols, *, timeframe):
                self.started = (tuple(symbols), timeframe)
                return {"status": "RUNNING"}
        class Overview:
            def get_overview(self):
                return {"markets": [{"symbol": "BTC_USDT"}, {"symbol": "ETH_USD"}]}
        fake_server = type("FakeServer", (), {})()
        fake_server.active_market_provider = None
        fake_server.cryptocom_market_overview = Overview()
        fake_server.cryptocom_market_stream = Stream()
        fake_server.live_signal_scanner = Scanner()
        result = start_local_public_market_tracking(fake_server)
        self.assertEqual(result["connection_status"], "CONNECTING")
        self.assertEqual(fake_server.cryptocom_market_stream.symbols, ["BTC_USDT", "ETH_USD"])
        self.assertEqual(fake_server.live_signal_scanner.started, (("BTC_USDT", "ETH_USD"), "5m"))
        self.assertEqual(fake_server.active_market_provider, "cryptocom")

    def test_foreign_origin_cannot_read_private_desk_state(self):
        request = Request(self.url + "/api/desk/state", headers={"Origin": "https://foreign.example"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(request)
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()

    def test_crypto_transaction_history_is_read_only_persisted_and_same_origin(self):
        class TransactionClient:
            def read_transactions(self, api_key, api_secret, *, start_time_ms, end_time_ms, limit):
                self.call = (api_key, api_secret, start_time_ms, end_time_ms, limit)
                at = datetime.fromtimestamp((start_time_ms + 1000) / 1000, timezone.utc)
                row = CryptoComTransaction("REALIZED_PNL", "123", Decimal("0"), Decimal("2"),
                    Decimal("2"), "456", "789", "BTC_USDT", "SELL", at,
                    "private/get-transactions", "a" * 64)
                return (row,), "b" * 64

        client = TransactionClient()
        self.server.cryptocom_portfolio_client = client
        class ObserverCredentials:
            available = True
            def load(self):
                from invest_ai.portfolio.cryptocom_credential_store import CryptoComCredentials
                return CryptoComCredentials("observer-read-key", "observer-read-secret")

        self.server.cryptocom_observer_credential_store = ObserverCredentials()
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        payload = {"use_observer_credentials": True,
            "start_time_ms": now_ms - 7 * 86_400_000, "end_time_ms": now_ms}
        headers = {"Content-Type": "application/json", "Origin": self.url}
        with urlopen(Request(self.url + "/api/cryptocom/portfolio/transactions",
                data=json.dumps(payload).encode(), headers=headers)) as response:
            result = json.load(response)["snapshot"]
        self.assertEqual(client.call[:2], ("observer-read-key", "observer-read-secret"))
        self.assertEqual(result["item_count"], 1)
        self.assertFalse(result["usable_for_risk"])
        self.assertFalse(result["orders_enabled"])
        with urlopen(self.url + "/api/desk/transactions") as response:
            saved = json.load(response)["snapshot"]
        self.assertEqual(saved["items"][0]["trade_id"], "789")
        self.assertEqual(saved["payload_sha256"], result["payload_sha256"])
        db = sqlite3.connect(self.server.desk_store.path)
        try:
            db.execute("UPDATE cryptocom_transaction_snapshots SET payload_json='[]'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(HTTPError) as refused:
            urlopen(self.url + "/api/desk/transactions")
        self.assertEqual(refused.exception.code, 503)
        refused.exception.close()

    def test_crypto_funding_history_is_persisted_paginated_and_observation_only(self):
        class FundingClient:
            def read_funding_history(self, api_key, api_secret, *, direction, start_time_ms,
                                     end_time_ms, page_size, max_pages):
                self.call = (api_key, api_secret, direction, start_time_ms, end_time_ms, page_size, max_pages)
                created = datetime.fromtimestamp((start_time_ms + 1000) / 1000, timezone.utc)
                item = SimpleNamespace(direction=direction, transfer_id="deposit-1", currency="USDT",
                    amount=Decimal("25.5"), fee=Decimal("0.1"), status="1", created_at=created,
                    updated_at=created, source=("private/get-deposit-history" if direction == "DEPOSIT"
                                                else "private/get-withdrawal-history"), payload_sha256="a" * 64)
                page = SimpleNamespace(page=0, page_size=page_size, items=(item,), payload_sha256="b" * 64)
                return SimpleNamespace(direction=direction, start_time_ms=start_time_ms, end_time_ms=end_time_ms,
                    pages=(page,), complete=True, next_page=None, request_count=1)

        client = FundingClient()
        self.server.cryptocom_portfolio_client = client
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        payload = {"api_key": "fixture-key", "api_secret": "fixture-secret", "direction": "DEPOSIT",
            "start_time_ms": now_ms - 86_400_000, "end_time_ms": now_ms}
        headers = {"Content-Type": "application/json", "Origin": self.url}
        with urlopen(Request(self.url + "/api/cryptocom/portfolio/funding-history",
                data=json.dumps(payload).encode(), headers=headers)) as response:
            result = json.load(response)["snapshot"]
        self.assertEqual(client.call[:3], ("fixture-key", "fixture-secret", "DEPOSIT"))
        self.assertEqual(result["item_count"], 1)
        self.assertTrue(result["complete"])
        self.assertFalse(result["usable_for_risk"])
        self.assertFalse(result["orders_enabled"])
        with urlopen(self.url + "/api/desk/funding-history?direction=DEPOSIT") as response:
            saved = json.load(response)["snapshot"]
        self.assertEqual(saved["items"][0]["transfer_id"], "deposit-1")
        self.assertEqual(saved["payload_sha256"], result["payload_sha256"])
        self.assertEqual(saved["items"][0]["currency"], "USDT")
        db = sqlite3.connect(self.server.desk_store.path)
        try:
            db.execute("UPDATE cryptocom_funding_history_snapshots SET payload_json='{}'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(HTTPError) as corrupted:
            urlopen(self.url + "/api/desk/funding-history?direction=DEPOSIT")
        self.assertEqual(corrupted.exception.code, 503)
        corrupted.exception.close()
        rejected = Request(self.url + "/api/cryptocom/portfolio/funding-history",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json",
                "Origin": "https://foreign.example"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(rejected)
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()

    def test_crypto_transaction_import_persists_each_partition_and_returns_batch_coverage(self):
        class PagedTransactionClient:
            def read_transactions_partitioned(self, api_key, api_secret, *, start_time_ms,
                                              end_time_ms, limit, max_requests):
                self.call = (api_key, api_secret, start_time_ms, end_time_ms, limit, max_requests)
                middle = start_time_ms + (end_time_ms - start_time_ms) // 2
                left_time = datetime.fromtimestamp((start_time_ms + 1) / 1000, timezone.utc)
                right_time = datetime.fromtimestamp((middle + 1) / 1000, timezone.utc)
                left = CryptoComTransaction("TRADING", "101", Decimal("1"), Decimal("10"),
                    Decimal("0"), "201", "301", "BTC_USDT", "BUY", left_time,
                    "private/get-transactions", "a" * 64)
                right = CryptoComTransaction("TRADE_FEE", "102", Decimal("0"), Decimal("0.1"),
                    Decimal("0"), "201", "301", "BTC_USDT", "BUY", right_time,
                    "private/get-transactions", "b" * 64)
                return CryptoComTransactionPageSet((
                    CryptoComTransactionPage(start_time_ms, middle, (left,), "c" * 64),
                    CryptoComTransactionPage(middle, end_time_ms, (right,), "d" * 64),
                ), True, (), 3)

        client = PagedTransactionClient()
        self.server.cryptocom_portfolio_client = client
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        payload = {"api_key": "fixture-key", "api_secret": "fixture-secret",
            "start_time_ms": now_ms - 7 * 86_400_000, "end_time_ms": now_ms}
        headers = {"Content-Type": "application/json", "Origin": self.url}
        with urlopen(Request(self.url + "/api/cryptocom/portfolio/transactions",
                data=json.dumps(payload).encode(), headers=headers)) as response:
            imported = json.load(response)["snapshot"]
        self.assertEqual(client.call[-2:], (100, 16))
        self.assertEqual(imported["page_count"], 2)
        self.assertEqual(imported["item_count"], 2)
        self.assertTrue(imported["coverage_complete"])
        self.assertEqual(imported["page_status"], "PAGINATED_COMPLETE")
        with urlopen(self.url + "/api/desk/transactions") as response:
            saved = json.load(response)["snapshot"]
        self.assertEqual(saved["items"], imported["items"])
        self.assertTrue(saved["coverage_complete"])
        pages = self.server.desk_store.cryptocom_transaction_history(limit=50)
        self.assertEqual(sum(page["batch_id"] == imported["batch_id"] for page in pages), 2)

    def test_crypto_executed_trades_are_local_hashed_and_never_risk_authority(self):
        class TradeClient:
            def read_trades(self, api_key, api_secret, *, start_time_ns, end_time_ns, limit):
                self.call = (api_key, api_secret, start_time_ns, end_time_ns, limit)
                occurred = datetime.fromtimestamp((start_time_ns + 5_000_000) / 1e9, timezone.utc)
                return (CryptoComExecutedTrade("789", "456", "321", "client-1", "BTC_USDT", "BUY",
                    "MAKER", Decimal("0.01"), Decimal("60000"), Decimal("-0.15"), "USDT", occurred,
                    str(start_time_ns + 5_000_000), "private/get-trades", "a" * 64),), "a" * 64

        client = TradeClient()
        self.server.cryptocom_portfolio_client = client
        now_ns = time.time_ns()
        payload = {"api_key": "fixture-key", "api_secret": "fixture-secret",
            "start_time_ns": str(now_ns - 3_600_000_000_000), "end_time_ns": str(now_ns)}
        headers = {"Content-Type": "application/json", "Origin": self.url}
        with urlopen(Request(self.url + "/api/cryptocom/portfolio/trades",
                data=json.dumps(payload).encode(), headers=headers)) as response:
            result = json.load(response)["snapshot"]
        self.assertEqual(client.call[:2], ("fixture-key", "fixture-secret"))
        self.assertEqual(result["item_count"], 1)
        self.assertFalse(result["usable_for_risk"])
        self.assertFalse(result["orders_enabled"])
        with urlopen(self.url + "/api/desk/trades") as response:
            saved = json.load(response)["snapshot"]
        self.assertEqual(saved["items"][0]["trade_id"], "789")
        self.assertEqual(saved["items"][0]["fee_currency"], "USDT")
        self.assertEqual(saved["payload_sha256"], result["payload_sha256"])
        rejected = Request(self.url + "/api/cryptocom/portfolio/trades", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://foreign.example"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(rejected)
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()
        db = sqlite3.connect(self.server.desk_store.path)
        try:
            db.execute("UPDATE cryptocom_trade_snapshots SET payload_json='{}'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(HTTPError) as refused:
            urlopen(self.url + "/api/desk/trades")
        self.assertEqual(refused.exception.code, 503)
        refused.exception.close()

    def test_paginated_crypto_trades_are_saved_as_verified_batch_and_reloaded_together(self):
        class PaginatedTradeClient:
            def read_trades_partitioned(self, api_key, api_secret, *, start_time_ns, end_time_ns, limit, max_requests):
                self.call = (api_key, api_secret, start_time_ns, end_time_ns, limit, max_requests)
                mid = start_time_ns + (end_time_ns - start_time_ns) // 2

                def fill(identity, timestamp_ns, digest):
                    return CryptoComExecutedTrade(str(identity), str(identity + 100), str(identity + 200), None,
                        "BTC_USDT", "BUY", "MAKER", Decimal("0.01"), Decimal("50000"), Decimal("-0.01"),
                        "USDT", datetime.fromtimestamp(timestamp_ns / 1_000_000_000, timezone.utc),
                        str(timestamp_ns), "private/get-trades", digest)

                first = fill(1, start_time_ns + 1, "b" * 64)
                second = fill(2, mid + 1, "c" * 64)
                return CryptoComTradePageSet((
                    CryptoComTradePage(start_time_ns, mid, (first,), "b" * 64),
                    CryptoComTradePage(mid, end_time_ns, (second,), "c" * 64),
                ), True, (), 3)

        client = PaginatedTradeClient()
        self.server.cryptocom_portfolio_client = client
        end_ns = time.time_ns()
        start_ns = end_ns - 7 * 86_400_000_000_000
        payload = {"api_key": "fixture-key", "api_secret": "fixture-secret",
            "start_time_ns": str(start_ns), "end_time_ns": str(end_ns)}
        headers = {"Content-Type": "application/json", "Origin": self.url}
        with urlopen(Request(self.url + "/api/cryptocom/portfolio/trades",
                data=json.dumps(payload).encode(), headers=headers)) as response:
            imported = json.load(response)["snapshot"]
        self.assertEqual(imported["page_status"], "PAGINATED_COMPLETE")
        self.assertEqual(imported["page_count"], 2)
        self.assertEqual(imported["item_count"], 2)
        self.assertTrue(imported["coverage_complete"])
        stored_pages = self.server.desk_store.cryptocom_trade_history(limit=50)
        self.assertEqual(sum(page.get("batch_id") == imported["batch_id"] for page in stored_pages), 2)
        reloaded = self.server.desk_store.latest_cryptocom_trades()
        self.assertEqual(reloaded["item_count"], 2)
        self.assertEqual(reloaded["page_status"], "PAGINATED_COMPLETE")
        with urlopen(self.url + "/api/desk/reconciliation") as response:
            report = json.load(response)["reconciliation"]
        self.assertEqual(report["executed_trade_count"], 2)
        self.assertFalse(report["usable_for_risk"])
        self.assertFalse(report["orders_enabled"])
        db = sqlite3.connect(self.server.desk_store.path)
        try:
            db.execute("DELETE FROM cryptocom_trade_snapshots WHERE id=(SELECT MAX(id) FROM cryptocom_trade_snapshots)")
            db.commit()
        finally:
            db.close()
        with urlopen(self.url + "/api/desk/trades") as response:
            partial = json.load(response)["snapshot"]
        self.assertEqual(partial["page_status"], "PAGINATED_INCOMPLETE")
        self.assertFalse(partial["persistence_complete"])
        with urlopen(self.url + "/api/desk/reconciliation") as response:
            report = json.load(response)["reconciliation"]
        self.assertIn("TRADE_BATCH_PAGE_MISSING", report["reasons"])

    def test_cash_history_is_persisted_as_observation_and_kept_out_of_risk(self):
        from types import SimpleNamespace
        from invest_ai.portfolio.cryptocom_readonly import CryptoComBalanceHistoryPoint

        class HistoryClient:
            def read_balance_history(self, api_key, api_secret, *, timeframe, limit):
                self.call = (api_key, api_secret, timeframe, limit)
                return SimpleNamespace(timeframe="H1", observed_at=datetime.now(timezone.utc),
                    points=(CryptoComBalanceHistoryPoint("EUR", datetime(2026, 10, 10, 10, tzinfo=timezone.utc), Decimal("12.30")),),
                    source="private/user-balance-history", payload_sha256="c" * 64)

        client = HistoryClient()
        self.server.cryptocom_portfolio_client = client
        payload = {"api_key": "fixture-key", "api_secret": "fixture-secret", "timeframe": "H1", "limit": 24}
        request = Request(self.url + "/api/cryptocom/portfolio/balance-history", data=json.dumps(payload).encode(),
            headers={"Origin": self.url, "Content-Type": "application/json"})
        with urlopen(request) as response:
            snapshot = json.load(response)["snapshot"]
        self.assertEqual(client.call, ("fixture-key", "fixture-secret", "H1", 24))
        self.assertEqual(snapshot["currency"], "EUR")
        self.assertEqual(snapshot["points"][0]["total_cash"], "12.30")
        self.assertFalse(snapshot["usable_for_risk"])
        self.assertFalse(snapshot["orders_enabled"])
        self.assertIn("écarts temporaires", snapshot["warning"])
        with urlopen(self.url + "/api/desk/balance-history") as response:
            saved = json.load(response)["snapshot"]
        self.assertEqual(saved["payload_sha256"], snapshot["payload_sha256"])
        self.assertEqual(saved["points"], snapshot["points"])

    def test_balance_history_route_rejects_cross_origin(self):
        request = Request(self.url + "/api/cryptocom/portfolio/balance-history",
            data=b'{"api_key":"k","api_secret":"s","timeframe":"H1","limit":24}',
            headers={"Origin": "https://foreign.example", "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(request)
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()

    def test_crypto_transaction_history_rejects_foreign_origin_and_overwide_window(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        base = {"api_key": "key", "api_secret": "secret", "start_time_ms": now_ms - 8 * 86_400_000,
            "end_time_ms": now_ms}
        headers = {"Content-Type": "application/json", "Origin": "https://foreign.example"}
        with self.assertRaises(HTTPError) as refused:
            urlopen(Request(self.url + "/api/cryptocom/portfolio/transactions",
                data=json.dumps(base).encode(), headers=headers))
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()
        headers["Origin"] = self.url
        with self.assertRaises(HTTPError) as refused:
            urlopen(Request(self.url + "/api/cryptocom/portfolio/transactions",
                data=json.dumps(base).encode(), headers=headers))
        self.assertEqual(refused.exception.code, 400)
        refused.exception.close()

    def test_crypto_spot_fee_schedule_is_read_only_and_same_origin(self):
        class FeeClient:
            def read_fee_schedule(self, api_key, api_secret):
                self.credentials = (api_key, api_secret)
                return CryptoComFeeSchedule(5, Decimal("25"), Decimal("50"), datetime.now(timezone.utc),
                    "private/get-fee-rate", "c" * 64)

        client = FeeClient()
        self.server.cryptocom_portfolio_client = client
        headers = {"Content-Type": "application/json", "Origin": self.url}
        payload = json.dumps({"api_key": "fixture-key", "api_secret": "fixture-secret"}).encode()
        with urlopen(Request(self.url + "/api/cryptocom/portfolio/fees", data=payload, headers=headers)) as response:
            result = json.load(response)
        self.assertEqual(client.credentials, ("fixture-key", "fixture-secret"))
        self.assertEqual(result["maker_rate"], "0.0025")
        self.assertEqual(result["taker_rate"], "0.005")
        self.assertFalse(result["usable_for_risk"])
        self.assertFalse(result["orders_enabled"])
        headers["Origin"] = "https://foreign.example"
        with self.assertRaises(HTTPError) as refused:
            urlopen(Request(self.url + "/api/cryptocom/portfolio/fees", data=payload, headers=headers))
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()

    def test_realtime_overview_merges_only_selected_websocket_pairs(self):
        class Stream:
            received_at = datetime.now(timezone.utc).isoformat()
            def disconnect(self):
                return {"connection_status": "DISCONNECTED"}
            def state(self):
                return {"connection_status": "CONNECTED", "symbols": ["BTC_USDT", "ETH_USDT"],
                    "quotes": [{"symbol": "BTC_USDT", "price": "62010", "provider_timestamp": "2026-10-09T20:00:01Z",
                        "received_at": self.received_at}]}
        stream = Stream()
        self.server.cryptocom_market_stream = stream
        with urlopen(self.url + "/api/market-data/crypto-overview") as response:
            result = json.load(response)
        btc = next(row for row in result["markets"] if row["symbol"] == "BTC_USDT")
        self.assertEqual(btc["instrument_rules"]["price_tick_size"], "0.01")
        self.assertEqual(btc["instrument_rules"]["quantity_tick_size"], "0.00001")
        self.assertEqual(result["instrument_catalog_status"], "CURRENT")
        self.assertEqual(btc["price"], "62010")
        self.assertEqual(btc["price_source"], "Crypto.com Exchange public ticker WebSocket")
        self.assertEqual(result["realtime"]["status"], "PARTIAL")
        self.assertEqual(result["realtime"]["updated_pairs"], 1)
        stream.received_at = "2020-01-01T00:00:00+00:00"
        with urlopen(self.url + "/api/market-data/crypto-overview") as response:
            stale = json.load(response)
        self.assertEqual(stale["realtime"]["status"], "REST")
        self.assertEqual(stale["markets"][0]["price"], "62000")

    def test_connect_all_subscribes_the_official_spot_list_without_account_credentials(self):
        class Stream:
            symbols = None
            def connect(self, symbols):
                self.symbols = symbols
                return {"connection_status": "SUBSCRIBING", "symbols": symbols.split(",")}
            def disconnect(self): return {"connection_status": "DISCONNECTED"}
            def state(self): return {"connection_status": "SUBSCRIBING", "quotes": []}
        stream = Stream()
        self.server.cryptocom_market_stream = stream
        class Scanner:
            started = None
            def start(self, symbols, *, timeframe):
                self.started = (tuple(symbols), timeframe)
                return {"status": "RUNNING"}
            def stop(self): return {"status": "STOPPED"}
        scanner = Scanner()
        self.server.live_signal_scanner = scanner
        payload = json.dumps({"provider": "cryptocom", "all_spot": True}).encode()
        request = Request(self.url + "/api/market-data/connect-all", data=payload,
            headers={"Origin": self.url, "Content-Type": "application/json"})
        with urlopen(request) as response:
            result = json.load(response)
            self.assertEqual(response.status, 202)
        self.assertEqual(stream.symbols, "BTC_USDT")
        self.assertEqual(result["connection_status"], "SUBSCRIBING")
        self.assertEqual(self.server.active_market_provider, "cryptocom")
        self.assertEqual(scanner.started, (("BTC_USDT",), "5m"))

    def test_disconnect_stops_scanner_and_public_stream(self):
        class Scanner:
            stopped = False
            def stop(self):
                self.stopped = True
                return {"status": "STOPPED"}
        class Stream:
            disconnected = False
            def disconnect(self):
                self.disconnected = True
                return {"connection_status": "DISCONNECTED"}
        scanner, stream = Scanner(), Stream()
        self.server.live_signal_scanner = scanner
        self.server.cryptocom_market_stream = stream
        self.server.active_market_provider = "cryptocom"
        request = Request(self.url + "/api/market-data/disconnect",
            data=b'{"confirm_disconnect":true}',
            headers={"Origin": self.url, "Content-Type": "application/json"})
        with urlopen(request) as response:
            result = json.load(response)
        self.assertTrue(scanner.stopped)
        self.assertTrue(stream.disconnected)
        self.assertIsNone(self.server.active_market_provider)
        self.assertFalse(result["orders_enabled"])

    def test_connect_all_rejects_foreign_origin_and_non_boolean_confirmation(self):
        headers = {"Origin": "https://foreign.example", "Content-Type": "application/json"}
        request = Request(self.url + "/api/market-data/connect-all",
            data=json.dumps({"provider": "cryptocom", "all_spot": True}).encode(), headers=headers)
        with self.assertRaises(HTTPError) as refused:
            urlopen(request)
        self.assertEqual(refused.exception.code, 403); refused.exception.close()
        request = Request(self.url + "/api/market-data/connect-all",
            data=b'{"provider":"cryptocom","all_spot":1}',
            headers={"Origin": self.url, "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(request)
        self.assertEqual(refused.exception.code, 400); refused.exception.close()

    def test_foreign_origin_cannot_read_exchange_overview(self):
        request = Request(self.url + "/api/market-data/crypto-overview", headers={"Origin": "https://foreign.example"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(request)
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()

    def test_spot_quote_evidence_is_archived_and_only_yields_a_price_candidate(self):
        from invest_ai.data.cryptocom_market_readonly import CryptoComMarketTick
        now = datetime.now(timezone.utc)
        tick = CryptoComMarketTick(
            "BTC_USD", Decimal("62001.25"), Decimal("62000"), Decimal("62002"), "COMPLETE",
            now, now, "USD", "Crypto.com Exchange", "Spot", "Crypto.com Exchange public ticker WebSocket", "a" * 64,
        )
        class Stream:
            def state(self):
                return {"connection_status": "CONNECTED", "quotes": [{
                    "symbol": tick.symbol, "price": str(tick.price), "last_trade_price": str(tick.price),
                    "best_bid": str(tick.best_bid), "best_ask": str(tick.best_ask), "quote_status": tick.quote_status,
                    "provider_timestamp": now.isoformat(), "received_at": now.isoformat(), "currency": tick.currency,
                    "exchange": tick.exchange, "instrument_type": tick.instrument_type, "source": tick.source,
                    "payload_sha256": tick.payload_sha256,
                }]}
            def disconnect(self): return {"connection_status": "DISCONNECTED"}
        self.server.cryptocom_market_stream = Stream()
        class Overview:
            def get_overview(self): return {"markets": [{"symbol": "BTC_USD"}]}
        self.server.cryptocom_market_overview = Overview()
        request = Request(self.url + "/api/market-evidence", data=json.dumps({"symbol": "BTC_USD"}).encode(),
            headers={"Origin": self.url, "Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)
            result = json.load(response)
        self.assertEqual(result["price"], "62001")
        self.assertTrue(result["eligible_for_risk_price_candidate"])
        self.assertEqual(result["authority_status"], "MARKET_PRICE_CANDIDATE_ONLY")
        self.assertFalse(result["orders_enabled"])
        record = self.server.market_evidence_store.get(result["snapshot_id"])
        self.assertEqual(record.snapshot.symbol, "BTC_USD")

    def test_market_evidence_refuses_pair_outside_current_spot_universe(self):
        self.server.cryptocom_market_stream = type("Stream", (), {
            "state": lambda _self: {"connection_status": "CONNECTED", "quotes": []},
            "disconnect": lambda _self: {"connection_status": "DISCONNECTED"}})()
        self.server.cryptocom_market_overview = type("Overview", (), {"get_overview": lambda _self: {
            "markets": [{"symbol": "BTC_USDT"}]}})()
        request = Request(self.url + "/api/market-evidence", data=json.dumps({"symbol": "BTC_USD"}).encode(),
            headers={"Origin": self.url, "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(request)
        self.assertEqual(refused.exception.code, 400)
        refused.exception.close()
        self.server.cryptocom_market_stream = type("Stream", (), {
            "state": lambda _self: {"connection_status": "CONNECTED", "quotes": []},
            "disconnect": lambda _self: {"connection_status": "DISCONNECTED"}})()

    def test_spot_quote_evidence_requires_same_origin_and_complete_bid_ask(self):
        from invest_ai.data.cryptocom_market_readonly import CryptoComMarketTick
        now = datetime.now(timezone.utc)
        tick = CryptoComMarketTick("BTC_USD", Decimal("62000"), Decimal("62000"), None, "INCOMPLETE",
            now, now, "USD", "Crypto.com Exchange", "Spot", "ticker", "a" * 64)
        class Stream:
            def state(self):
                return {"connection_status": "CONNECTED", "quotes": [{
                    "symbol": tick.symbol, "last_trade_price": str(tick.price), "best_bid": str(tick.best_bid),
                    "best_ask": None, "quote_status": tick.quote_status, "provider_timestamp": now.isoformat(),
                    "received_at": now.isoformat(), "currency": tick.currency, "exchange": tick.exchange,
                    "instrument_type": tick.instrument_type, "source": tick.source, "payload_sha256": tick.payload_sha256,
                }]}
            def disconnect(self): return {"connection_status": "DISCONNECTED"}
        self.server.cryptocom_market_stream = Stream()
        self.server.cryptocom_market_overview = type("Overview", (), {"get_overview": lambda _self: {
            "markets": [{"symbol": "BTC_USD"}]}})()
        body = json.dumps({"symbol": "BTC_USD"}).encode()
        request = Request(self.url + "/api/market-evidence", data=body,
            headers={"Origin": "https://foreign.example", "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(request)
        self.assertEqual(refused.exception.code, 403); refused.exception.close()
        request = Request(self.url + "/api/market-evidence", data=body,
            headers={"Origin": self.url, "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(request)
        self.assertEqual(refused.exception.code, 409); refused.exception.close()

    def test_crypto_portfolio_snapshot_is_persisted_separately_from_risk_authority(self):
        saved = self.server.desk_store.record_cryptocom_portfolio({
            "provider": "Crypto.com Exchange", "fetched_at": "2026-10-09T20:00:00Z",
            "balances": [], "wallet_balances": [], "positions": [], "open_orders": [{"order_id": "42"}],
            "payload_sha256": "a" * 64,
            "persistent": False, "usable_for_risk": False, "orders_enabled": False,
        })
        self.assertTrue(saved["persistent"])
        self.assertEqual(saved["authority_status"], "OBSERVATION_ONLY")
        self.assertFalse(saved["usable_for_risk"])
        self.assertFalse(saved["usable_for_execution"])
        with urlopen(self.url + "/api/desk/portfolio") as response:
            result = json.load(response)["snapshot"]
        self.assertEqual(result["snapshot_id"], saved["snapshot_id"])
        self.assertEqual(result["source_payload_sha256"], "a" * 64)
        self.assertEqual(result["open_orders"], [{"order_id": "42"}])
        self.assertFalse(result["orders_enabled"])

    def test_spot_portfolio_candidate_requires_explicit_currency_and_live_verified_prices(self):
        now = datetime.now(timezone.utc)
        digest = "a" * 64
        self.server.desk_store.record_cryptocom_portfolio({
            "provider": "Crypto.com Exchange", "source": "private/user-balance",
            "fetched_at": now.isoformat(), "payload_sha256": digest,
            "positions_payload_sha256": digest, "open_orders_payload_sha256": digest,
            "balances": [{"currency": "USD", "available": "1000", "total_margin": "1000",
                "initial_margin": "0", "total_cash": "1100", "total_borrow": "0", "isolated_cash": "0",
                "is_liquidating": False, "has_isolated_positions": False, "source": "private/user-balance",
                "payload_sha256": digest}],
            "wallet_balances": [
                {"currency": "USD", "account_currency": "USD", "quantity": "500", "market_value": "500",
                 "collateral_amount": "500", "max_withdrawal_balance": "500", "reserved_quantity": "0",
                 "source": "private/user-balance", "payload_sha256": digest},
                {"currency": "BTC", "account_currency": "USD", "quantity": "0.01", "market_value": "600",
                 "collateral_amount": "600", "max_withdrawal_balance": "0.01", "reserved_quantity": "0",
                 "source": "private/user-balance", "payload_sha256": digest}],
            "positions": [], "open_orders": [],
        })
        from invest_ai.data.cryptocom_market_readonly import CryptoComMarketTick
        tick = CryptoComMarketTick("BTC_USD", Decimal("60000"), Decimal("59999"), Decimal("60001"),
            "COMPLETE", now, now, "USD", "Crypto.com Exchange", "Spot", "ticker", "b" * 64)
        class Stream:
            def state(self): return {"connection_status": "CONNECTED", "quotes": [{
                "symbol": "BTC_USD", "quote_status": "COMPLETE", "exchange": "Crypto.com Exchange",
                "instrument_type": "Spot"}]}
            def get_tick(self, symbol): return tick if symbol == "BTC_USD" else None
            def disconnect(self): return {"connection_status": "DISCONNECTED"}
        self.server.cryptocom_market_stream = Stream()
        self.server.cryptocom_market_overview = type("Overview", (), {"get_overview": lambda _self: {
            "markets": [{"symbol": "BTC_USD"}]}})()
        request = Request(self.url + "/api/cryptocom/portfolio/candidate",
            data=json.dumps({"base_currency": "USD"}).encode(),
            headers={"Origin": self.url, "Content-Type": "application/json"})
        with urlopen(request) as response:
            result = json.load(response)
        self.assertEqual(result["total_equity"], "1100.00")
        self.assertEqual(result["authority_status"], "BROKER_SPOT_PORTFOLIO_CANDIDATE_ONLY")
        self.assertFalse(result["usable_for_risk"])
        self.assertFalse(result["orders_enabled"])

    def test_portfolio_candidate_route_rejects_cross_origin_and_implicit_currency(self):
        for body, origin, expected in (({"base_currency": "USD"}, "https://foreign.example", 403),
                                       ({}, self.url, 400), ({"base_currency": "USDT"}, self.url, 400)):
            request = Request(self.url + "/api/cryptocom/portfolio/candidate", data=json.dumps(body).encode(),
                headers={"Origin": origin, "Content-Type": "application/json"})
            with self.subTest(body=body, origin=origin), self.assertRaises(HTTPError) as refused:
                urlopen(request)
            self.assertEqual(refused.exception.code, expected)
            refused.exception.close()

    def test_risk_limit_draft_is_validated_persisted_and_never_active(self):
        payload = {"base_currency": "EUR", "capital_limit": "1000,50",
                   "max_order_notional": "100,25", "max_daily_loss": "25,5"}
        request = Request(self.url + "/api/desk/risk-limits", data=json.dumps(payload).encode(),
            headers={"Origin": self.url, "Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)
            candidate = json.load(response)["candidate"]
        self.assertEqual(candidate["approval_status"], "PENDING_OWNER_REVIEW")
        self.assertFalse(candidate["active"])
        self.assertFalse(candidate["orders_enabled"])
        self.assertEqual(candidate["capital_limit"], "1000.50")
        with urlopen(self.url + "/api/desk/risk-limits") as response:
            stored = json.load(response)["candidate"]
        self.assertEqual(stored["payload_sha256"], candidate["payload_sha256"])
        for invalid in (
            {**payload, "max_order_notional": "1001"},
            {**payload, "max_daily_loss": "1001"},
            {**payload, "capital_limit": 1000},
        ):
            request = Request(self.url + "/api/desk/risk-limits", data=json.dumps(invalid).encode(),
                headers={"Origin": self.url, "Content-Type": "application/json"})
            with self.subTest(invalid=invalid), self.assertRaises(HTTPError) as refused:
                urlopen(request)
            self.assertEqual(refused.exception.code, 400)
            refused.exception.close()
        request = Request(self.url + "/api/desk/risk-limits", data=json.dumps(payload).encode(),
            headers={"Origin": "https://foreign.example", "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(request)
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()
        db = sqlite3.connect(Path(self.temp.name) / "desk.sqlite3")
        try:
            db.execute("UPDATE risk_limit_candidates SET capital_limit='9999'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(HTTPError) as refused:
            urlopen(self.url + "/api/desk/risk-limits")
        self.assertEqual(refused.exception.code, 503)
        refused.exception.close()

    def test_invalid_watchlist_does_not_mutate_database(self):
        for payload in ({"symbol": "BTC_USDT", "enabled": "yes"},
                        {"symbol": "../../bad", "enabled": True}, {"symbol": "BTC_USDT", "enabled": True, "owner": True}):
            with self.assertRaises(HTTPError) as refused:
                urlopen(Request(self.url + "/api/desk/watchlist", data=json.dumps(payload).encode(),
                    headers={"Origin": self.url, "Content-Type": "application/json"}))
            self.assertEqual(refused.exception.code, 400)
            refused.exception.close()
        self.assertEqual(self.server.desk_store.watchlist(), [])


class CandleTests(unittest.TestCase):
    def test_validates_provider_identity_values_and_sanitizes_errors(self):
        from bubus.market import CandleSource

        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _): return json.dumps(self.payload).encode()
        row = {"t": 1791570000000, "o": "1", "h": "2", "l": "1", "c": "1.5", "v": "10"}
        payload = {"code": 0, "result": {"instrument_name": "BTC_USDT", "data": [row]}}
        source = CandleSource(opener=lambda *a, **kw: Response(payload))
        self.assertEqual(source.get("BTC_USDT", "1h")["candles"][0]["c"], "1.5")
        payload["result"]["instrument_name"] = "ETH_USDT"
        with self.assertRaisesRegex(ValueError, "indisponible ou invalide"):
            CandleSource(opener=lambda *a, **kw: Response(payload)).get("BTC_USDT", "1h")
        with self.assertRaises(ValueError): source.get("../../secret", "1h")
        payload["result"]["instrument_name"] = "BTC_USDT"; row["h"] = "0"
        with self.assertRaises(ValueError):
            CandleSource(opener=lambda *a, **kw: Response(payload)).get("BTC_USDT", "1h")

    def test_independent_candle_requests_do_not_hold_the_cache_lock_during_network_io(self):
        from concurrent.futures import ThreadPoolExecutor
        from urllib.parse import parse_qs, urlsplit
        from bubus.market import CandleSource

        barrier = threading.Barrier(2)

        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _): return json.dumps(self.payload).encode()

        def opener(request, **_kwargs):
            symbol = parse_qs(urlsplit(request.full_url).query)["instrument_name"][0]
            barrier.wait(timeout=1)
            row = {"t": 1791570000000, "o": "1", "h": "2", "l": "1", "c": "1.5", "v": "10"}
            return Response({"code": 0, "result": {"instrument_name": symbol, "data": [row]}})

        source = CandleSource(opener=opener)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda symbol: source.get(symbol, "1h"), ("BTC_USDT", "ETH_USDT")))
        self.assertEqual([result["symbol"] for result in results], ["BTC_USDT", "ETH_USDT"])
