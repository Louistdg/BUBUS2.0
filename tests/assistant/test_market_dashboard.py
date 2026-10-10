from __future__ import annotations

import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from invest_ai.assistant.market_dashboard import make_server
from invest_ai.data.cryptocom_market_readonly import CryptoComPublicMarketStream
from invest_ai.data.cryptocom_market_overview import CryptoComMarketOverviewError
from invest_ai.data.coingecko_market_overview import CoinGeckoMarketOverviewError
from invest_ai.data.twelvedata_readonly import TwelveDataReadOnlyQuoteStream
from bubus.portfolio_observer import ReadOnlyPortfolioObserver


class FakeSocket:
    def __init__(self, url, *, on_open, on_message, on_error, on_close):
        self.url = url
        self.on_open = on_open
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def run_forever(self, **_kwargs):
        self.on_open(self)
        self.on_message(self, json.dumps({
            "event": "price", "symbol": "AAPL", "price": "230.15",
            "timestamp": 1791570000, "currency": "USD", "exchange": "NASDAQ",
            "type": "Common Stock",
        }))
        self.ready.set()
        self.closed.wait(2)

    def close(self):
        self.closed.set()
        self.on_close(self, 1000, "closed")


class FakeCryptoSocket(FakeSocket):
    def run_forever(self, **_kwargs):
        self.on_open(self)
        self.on_message(self, json.dumps({
            "id": -1, "method": "subscribe", "code": 0,
            "result": {"channel": "ticker", "instrument_name": "BTC_USDT",
                       "data": [{"i": "BTC_USDT", "a": "62001.25", "t": 1791570000123}]},
        }))
        self.ready.set()
        self.closed.wait(2)


class FakeCryptoOverview:
    def get_overview(self):
        return {"provider": "Crypto.com Exchange", "count": 1, "available_spot_count": 1,
                "markets": [{"symbol": "BTC_USDT", "price": "62000", "currency": "USDT",
                             "change_24h_pct": "1.2", "volume_24h_usd": "1000",
                             "provider_timestamp": "2026-10-09T20:00:00.000Z",
                             "source": "Crypto.com Exchange public/get-tickers", "payload_sha256": "a" * 64}],
                "fetched_at": "2026-10-09T20:00:00.000Z", "persistent": False,
                "refresh_after_seconds": 15, "usable_for_risk": False, "orders_enabled": False}


class FakeCoinGeckoOverview:
    def get_overview(self):
        return {"provider": "CoinGecko", "count": 1, "attribution_text": "Données fournies par CoinGecko",
                "attribution_url": "https://www.coingecko.com/", "markets": [{
                    "id": "bitcoin", "symbol": "BTC", "name": "Bitcoin", "price_usd": "62000",
                    "market_cap_usd": "1000000", "market_cap_rank": 1, "volume_24h_usd": "1000",
                    "change_24h_pct": "1.2", "provider_timestamp": "2026-10-09T20:00:00.000Z",
                    "source": "CoinGecko API /coins/markets", "payload_sha256": "b" * 64}],
                "fetched_at": "2026-10-09T20:00:00.000Z", "persistent": False,
                "refresh_after_seconds": 60, "usable_for_risk": False, "orders_enabled": False}


class MarketDashboardHttpTests(unittest.TestCase):
    def setUp(self):
        self.sockets = []

        def factory(url, **callbacks):
            socket = FakeSocket(url, **callbacks)
            self.sockets.append(socket)
            return socket

        quote_stream = TwelveDataReadOnlyQuoteStream(websocket_factory=factory)
        crypto_sockets = []

        def crypto_factory(url, **callbacks):
            socket = FakeCryptoSocket(url, **callbacks)
            crypto_sockets.append(socket)
            return socket

        self.crypto_sockets = crypto_sockets
        crypto_stream = CryptoComPublicMarketStream(websocket_factory=crypto_factory, sleeper=lambda _seconds: None)
        self.server = make_server(0, market_quote_stream=quote_stream, cryptocom_market_stream=crypto_stream,
                                  cryptocom_market_overview=FakeCryptoOverview(),
                                  coingecko_market_overview=FakeCoinGeckoOverview())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _post(self, path, payload, *, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        request = Request(self.base + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
        return urlopen(request, timeout=2)

    def test_dashboard_has_only_real_data_panels_and_no_demo_or_order_routes(self):
        with urlopen(self.base + "/", timeout=2) as response:
            page = response.read().decode("utf-8")
        self.assertIn("Marchés connectés", page)
        self.assertIn("crypto-dashboard-balances", page)
        self.assertIn("crypto-overview-rows", page)
        self.assertIn("coingecko-overview-rows", page)
        self.assertIn("Données fournies par CoinGecko", page)
        self.assertIn("Grandes actions à suivre", page)
        self.assertIn("Les widgets gratuits TradingView", page)
        self.assertIn("crypto-dashboard-positions", page)
        self.assertIn("cryptocom-remember-credentials", page)
        self.assertIn("Lire / actualiser le portefeuille", page)
        self.assertIn("Oublier la clé enregistrée", page)
        self.assertNotIn("SYNTH", page)
        self.assertNotIn("PAPER", page)
        self.assertNotIn("ORDRE DE DÉMONSTRATION", page)
        self.assertNotIn("/api/simulate", page)
        for path in ("/assets/market-dashboard.css", "/assets/market-dashboard.js"):
            with urlopen(self.base + path, timeout=2) as response:
                self.assertEqual(response.status, 200)
        for path in ("/api/demo-state", "/api/demo-portfolio", "/api/market-monitor-demo", "/api/strategy-demo"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                urlopen(self.base + path, timeout=2)
            self.assertEqual(raised.exception.code, 404)
            raised.exception.close()
        with self.assertRaises(HTTPError) as raised:
            self._post("/api/simulate", {"side": "BUY"}, origin=self.base)
        self.assertEqual(raised.exception.code, 404)
        raised.exception.close()

    def test_server_close_waits_for_observer_before_closing_streams(self):
        self.server.shutdown()
        self.thread.join(timeout=1)
        entered = threading.Event()
        release = threading.Event()
        disconnected = threading.Event()

        def collect():
            entered.set()
            release.wait(2)

        class Stream:
            def disconnect(self):
                disconnected.set()

        self.server.cryptocom_portfolio_observer = ReadOnlyPortfolioObserver(collect)
        self.server.market_quote_stream = Stream()
        self.server.cryptocom_portfolio_observer.start()
        self.assertTrue(entered.wait(1))
        closer = threading.Thread(target=self.server.server_close)
        closer.start()
        time.sleep(0.05)
        self.assertTrue(closer.is_alive())
        self.assertFalse(disconnected.is_set())
        release.set()
        closer.join(1)
        self.assertFalse(closer.is_alive())
        self.assertTrue(disconnected.is_set())

    def test_crypto_overview_is_loaded_from_local_same_origin_route(self):
        request = Request(self.base + "/api/market-data/crypto-overview", headers={"Origin": self.base})
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(payload["markets"][0]["symbol"], "BTC_USDT")
        self.assertFalse(payload["persistent"])
        denied = Request(self.base + "/api/market-data/crypto-overview", headers={"Origin": "http://evil.example"})
        with self.assertRaises(HTTPError) as raised:
            urlopen(denied, timeout=2)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_coingecko_overview_is_local_same_origin_and_attributed(self):
        request = Request(self.base + "/api/market-data/coingecko-overview", headers={"Origin": self.base})
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(payload["markets"][0]["id"], "bitcoin")
        self.assertEqual(payload["attribution_url"], "https://www.coingecko.com/")
        self.assertFalse(payload["persistent"])
        denied = Request(self.base + "/api/market-data/coingecko-overview", headers={"Origin": "http://evil.example"})
        with self.assertRaises(HTTPError) as raised:
            urlopen(denied, timeout=2)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_market_connection_requires_same_origin_and_never_echoes_api_key(self):
        secret = "private-market-key"
        with self.assertRaises(HTTPError) as raised:
            self._post("/api/market-data/connect", {"api_key": secret, "symbols": "AAPL"}, origin="http://evil.example")
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        self.assertEqual(self.sockets, [])

        with self._post("/api/market-data/connect", {"api_key": secret, "symbols": "AAPL"}, origin=self.base) as response:
            body = response.read()
            self.assertEqual(response.status, 202)
        self.assertNotIn(secret.encode(), body)
        self.assertTrue(self.sockets[0].ready.wait(1))
        with urlopen(self.base + "/api/market-data/state", timeout=2) as response:
            state = json.loads(response.read())
        self.assertEqual(state["provider"], "Twelve Data")
        self.assertEqual(state["quotes"][0]["price"], "230.15")
        self.assertEqual(state["quotes"][0]["exchange"], "NASDAQ")
        self.assertNotIn(secret, json.dumps(state))
        self.assertNotIn("apikey=", json.dumps(state))
        self.assertFalse(state["persistent"])
        self.assertFalse(state["orders_enabled"])

        with self._post("/api/market-data/disconnect", {"confirm_disconnect": True}, origin=self.base) as response:
            stopped = json.loads(response.read())
        self.assertEqual(stopped["connection_status"], "DISCONNECTED")
        self.assertEqual(stopped["quotes"], [])

    def test_cryptocom_public_market_stream_uses_no_api_or_account_credentials(self):
        with self._post("/api/market-data/connect", {
            "provider": "cryptocom", "symbols": "BTC_USDT",
        }, origin=self.base) as response:
            initial = json.loads(response.read())
            self.assertEqual(response.status, 202)
        self.assertEqual(initial["provider"], "Crypto.com Exchange")
        self.assertTrue(self.crypto_sockets[0].ready.wait(1))

        with self.assertRaises(HTTPError) as raised:
            self._post("/api/market-data/connect", {
                "provider": "cryptocom", "symbols": "BTC_USDT", "api_key": "must-not-be-used",
            }, origin=self.base)
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()
        raised.exception.close()

        with urlopen(self.base + "/api/market-data/state", timeout=2) as response:
            state = json.loads(response.read())
        self.assertEqual(state["provider"], "Crypto.com Exchange")
        self.assertEqual(state["quotes"][0]["price"], "62001.25")
        self.assertFalse(state["orders_enabled"])
        self.assertNotIn("api_key", json.dumps(state))

        with self._post("/api/market-data/disconnect", {"confirm_disconnect": True}, origin=self.base) as response:
            stopped = json.loads(response.read())
        self.assertEqual(stopped["connection_status"], "DISCONNECTED")


if __name__ == "__main__":
    unittest.main()
