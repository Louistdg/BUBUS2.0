"""« Ma liste » : note de risque 1 à 5, cryptos et actions, en ligne et sur le PC."""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from bubus.store import DeskStore
from bubus.watch_risk import (
    WatchRisk, WatchRiskError, risk_profile, yahoo_crypto_daily, yahoo_crypto_symbol, yahoo_daily, yahoo_search,
)
from tests.bubus_v2.test_cloud_app import _ServerCase


def wave(amplitude: float, days: int = 120, start: float = 100.0) -> list[float]:
    """Prix qui oscille chaque jour de ±amplitude (en fraction)."""
    return [start * (1 + amplitude * (1 if day % 2 else -1)) for day in range(days)]


class RiskProfileTests(unittest.TestCase):
    def test_score_rises_with_agitation(self):
        calm = risk_profile(wave(0.002), kind="action")
        wild = risk_profile(wave(0.08), kind="crypto")
        self.assertEqual((calm["score"], calm["label"]), (1, "Très calme"))
        self.assertEqual((wild["score"], wild["label"]), (5, "Très agité"))
        self.assertIn("par jour", wild["explanation"])

    def test_big_drop_alone_raises_the_score(self):
        closes = [100.0] * 60 + [100 - day for day in range(1, 41)]  # baisse lente de 40 %
        profile = risk_profile(closes, kind="action")
        self.assertGreaterEqual(profile["max_drop_3m_pct"], 39)
        self.assertEqual(profile["score"], 4)

    def test_changes_use_the_right_periods(self):
        closes = [100.0] * 100 + [110.0]
        crypto = risk_profile(closes, kind="crypto")
        self.assertEqual(crypto["change_1d_pct"], 10.0)
        self.assertEqual(crypto["change_1w_pct"], 10.0)
        self.assertEqual(crypto["change_1m_pct"], 10.0)

    def test_short_or_broken_history_is_refused(self):
        with self.assertRaises(WatchRiskError):
            risk_profile([1.0] * 10, kind="crypto")
        with self.assertRaises(WatchRiskError):
            risk_profile([math.nan, -1.0, 0.0] * 10, kind="crypto")


class FakeResponse:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit):
        return self.raw


class YahooParsingTests(unittest.TestCase):
    def test_chart_and_search_are_parsed_and_filtered(self):
        chart = {"chart": {"result": [{"meta": {"currency": "EUR", "longName": "LVMH", "regularMarketPrice": 390.0},
                                       "indicators": {"quote": [{"close": [380.0, None, 385.0]}]}}]}}
        seen = []
        data = yahoo_daily("MC.PA", opener=lambda request, **_: seen.append(request.full_url) or FakeResponse(chart))
        self.assertEqual((data["closes"], data["currency"], data["name"]), ([380.0, 390.0], "EUR", "LVMH"))
        self.assertIn("/MC.PA?", seen[0])
        search = {"quotes": [{"symbol": "MC.PA", "quoteType": "EQUITY", "longname": "LVMH", "exchDisp": "Paris"},
                             {"symbol": "BTC-USD", "quoteType": "CRYPTOCURRENCY", "shortname": "Bitcoin USD"},
                             {"symbol": "BTC-JPY", "quoteType": "CRYPTOCURRENCY"},
                             {"symbol": "bad symbol!", "quoteType": "EQUITY"}]}
        found = yahoo_search("lvmh", opener=lambda *_a, **_k: FakeResponse(search))
        self.assertEqual(found, [{"kind": "action", "symbol": "MC.PA", "name": "LVMH", "place": "Paris"},
                                 {"kind": "crypto", "symbol": "BTC_USD", "name": "Bitcoin", "place": "Crypto"}])


class WatchRiskServiceTests(unittest.TestCase):
    def test_describe_caches_and_reports_errors_in_french(self):
        calls = []

        def stock(symbol):
            calls.append(symbol)
            if symbol == "NOPE":
                raise OSError("réseau")
            return {"closes": wave(0.01), "price": 101.0, "currency": "USD", "name": "Apple", "source": "Yahoo Finance"}

        clock = [0.0]
        service = WatchRisk(stock_daily=stock, clock=lambda: clock[0])
        first = service.describe("action", "AAPL")
        self.assertEqual((first["name"], first["currency"]), ("Apple", "USD"))
        service.describe("action", "AAPL")
        self.assertEqual(calls, ["AAPL"])
        clock[0] = 601
        service.describe("action", "AAPL")
        self.assertEqual(calls, ["AAPL", "AAPL"])
        failed = service.describe("action", "NOPE")
        self.assertIn("Cours indisponible", failed["error"])
        self.assertNotIn("score", failed)

    def test_crypto_prices_come_from_yahoo(self):
        seen = []
        chart = {"chart": {"result": [{"meta": {"currency": "USD", "longName": "Bitcoin USD"},
                                       "indicators": {"quote": [{"close": wave(0.03)}]}}]}}
        service = WatchRisk(crypto_daily=lambda symbol: yahoo_crypto_daily(
            symbol, opener=lambda request, **_: seen.append(request.full_url) or FakeResponse(chart)))
        result = service.describe("crypto", "BTC_USDT", "BTC")
        self.assertEqual((result["source"], result["currency"], result["name"]), ("Yahoo Finance", "USD", "BTC"))
        self.assertIn("/BTC-USD?", seen[0])
        self.assertIn(result["score"], range(1, 6))
        self.assertEqual(yahoo_crypto_symbol("ETH_EUR"), "ETH-EUR")
        with self.assertRaises(WatchRiskError):
            yahoo_crypto_symbol("ETH_BTC")

    def test_search_needs_two_letters_and_explains_failures(self):
        service = WatchRisk(searcher=lambda text: (_ for _ in ()).throw(OSError()))
        self.assertEqual(service.search("a"), [])
        with self.assertRaises(WatchRiskError):
            service.search("apple")


class StockStoreTests(unittest.TestCase):
    def test_stock_list_is_validated_limited_and_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeskStore(Path(tmp) / "desk.sqlite3")
            store.set_watch("BTC_USD", True)
            store.set_stock_watch("MC.PA", "  LVMH   Paris ", True)
            self.assertEqual(store.my_list(), [{"kind": "crypto", "symbol": "BTC_USD", "name": "BTC"},
                                               {"kind": "action", "symbol": "MC.PA", "name": "LVMH Paris"}])
            for bad in ("mc.pa", "AAPL; DROP", "", "A" * 20):
                with self.assertRaises(ValueError):
                    store.set_stock_watch(bad, "x", True)
            store.set_stock_watch("MC.PA", "", False)
            self.assertEqual(store.stock_watchlist(), [])
            for index in range(50):
                store.set_stock_watch(f"S{index}", "x", True)
            with self.assertRaises(ValueError):
                store.set_stock_watch("ONEMORE", "x", True)


class FakeWatchRisk:
    def describe_all(self, items):
        return [{**item, "score": 2, "label": "Calme"} for item in items]

    def search(self, text):
        return [{"kind": "action", "symbol": "AAPL", "name": "Apple", "place": "NASDAQ"}]


class OnlineMyListTests(_ServerCase):
    def setUp(self):
        super().setUp()
        self.server.watch_risk = FakeWatchRisk()

    def post(self, payload, cookie, origin=None):
        return self.request("POST", "/api/desk/my-list", json.dumps(payload).encode(), {
            "Cookie": cookie, "Content-Type": "application/json",
            "Origin": origin or f"http://127.0.0.1:{self.port}"})

    def test_add_list_search_and_remove_online(self):
        cookie = self.session_cookie()
        self.assertEqual(self.request("GET", "/api/desk/my-list")[0], 401)
        status, _, body = self.post({"kind": "action", "symbol": "AAPL", "name": "Apple", "enabled": True}, cookie)
        self.assertEqual(status, 200, body)
        status, _, body = self.post({"kind": "crypto", "symbol": "BTC_USD", "enabled": True}, cookie)
        self.assertEqual(json.loads(body)["watchlist"], ["BTC_USD"])
        status, _, body = self.request("GET", "/api/desk/my-list", headers={"Cookie": cookie})
        result = json.loads(body)
        self.assertEqual([item["symbol"] for item in result["items"]], ["BTC_USD", "AAPL"])
        self.assertIn("ni un conseil", result["note"])
        self.assertFalse(result["orders_enabled"])
        status, _, body = self.request("GET", "/api/desk/my-list/search?q=apple", headers={"Cookie": cookie})
        self.assertEqual(json.loads(body)["results"][0]["symbol"], "AAPL")
        status, _, body = self.post({"kind": "action", "symbol": "AAPL", "enabled": False}, cookie)
        self.assertEqual([item["symbol"] for item in json.loads(body)["list"]], ["BTC_USD"])

    def test_bad_or_foreign_requests_are_refused(self):
        cookie = self.session_cookie()
        status, _, _ = self.post({"kind": "action", "symbol": "AAPL", "enabled": True}, cookie,
                                 origin="https://evil.example")
        self.assertEqual(status, 403)
        for payload in ({"kind": "ordre", "symbol": "AAPL", "enabled": True},
                        {"kind": "action", "symbol": "aapl", "enabled": True},
                        {"kind": "action", "symbol": "AAPL", "enabled": "oui"},
                        {"kind": "action", "symbol": "AAPL", "enabled": True, "quantity": 5}):
            self.assertEqual(self.post(payload, cookie)[0], 400, payload)
        self.assertEqual(self.server.desk_store.my_list(), [])


if __name__ == "__main__":
    unittest.main()
