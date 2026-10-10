from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from invest_ai.data.cryptocom_market_overview import CryptoComMarketOverview, CryptoComMarketOverviewError


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, amount=-1):
        return self.body[:amount]


class CryptoComMarketOverviewTests(unittest.TestCase):
    def test_filters_to_spot_pairs_sorts_caps_and_marks_read_only(self):
        rows = [
            {"i": "BTC_USDT", "a": "60000", "b": "59999", "k": "60001", "vv": "500", "c": "0.1", "t": 1791570000000},
            {"i": "ETH_USD", "a": "3000", "b": "3001", "k": "3000", "vv": "2000", "c": "-0.02", "t": 1791570000001},
            {"i": "BTCUSD-PERP", "a": "60001", "vv": "9000", "c": "0", "t": 1791570000002},
            {"i": "BAD_USD", "a": "NaN", "vv": "1", "c": "0", "t": 1791570000002},
        ]
        client = CryptoComMarketOverview(
            opener=lambda request, timeout: FakeResponse(json.dumps({"code": 0, "result": {"data": rows}}).encode()),
            clock=lambda: 1.0,
            utc_clock=lambda: datetime(2026, 10, 9, tzinfo=timezone.utc),
        )
        snapshot = client.get_overview()
        self.assertEqual([item["symbol"] for item in snapshot["markets"]], ["ETH_USD", "BTC_USDT"])
        self.assertEqual(snapshot["markets"][0]["change_24h_pct"], "-2.00")
        self.assertEqual(snapshot["markets"][0]["currency"], "USD")
        self.assertEqual(snapshot["markets"][0]["last_trade_price"], "3000")
        self.assertEqual(snapshot["markets"][0]["best_bid"], "3001")
        self.assertEqual(snapshot["markets"][0]["best_ask"], "3000")
        self.assertEqual(snapshot["markets"][0]["quote_status"], "CROSSED")
        self.assertEqual(snapshot["count"], 2)
        self.assertEqual(snapshot["available_spot_count"], 2)
        self.assertFalse(snapshot["persistent"])
        self.assertFalse(snapshot["usable_for_risk"])
        self.assertFalse(snapshot["orders_enabled"])
        self.assertEqual(len(snapshot["markets"][0]["payload_sha256"]), 64)

    def test_missing_bid_ask_is_explicitly_incomplete(self):
        row = {"i": "BTC_USD", "a": "60000", "vv": "1", "c": "0", "t": 1791570000000}
        client = CryptoComMarketOverview(
            opener=lambda *_args, **_kwargs: FakeResponse(json.dumps({"code": 0, "result": {"data": [row]}}).encode()),
            clock=lambda: 1.0,
        )
        market = client.get_overview()["markets"][0]
        self.assertEqual(market["last_trade_price"], "60000")
        self.assertIsNone(market["best_bid"])
        self.assertIsNone(market["best_ask"])
        self.assertEqual(market["quote_status"], "INCOMPLETE")

    def test_requests_public_fixed_url_without_credentials_and_caches_temporarily(self):
        calls = []
        clock = [10.0]
        body = json.dumps({"code": 0, "result": {"data": [
            {"i": "BTC_USD", "a": "60000", "vv": "1", "c": "0", "t": 1791570000000},
        ]}}).encode()
        def opener(request, timeout):
            calls.append((request.full_url, request.headers, timeout))
            return FakeResponse(body)
        client = CryptoComMarketOverview(opener=opener, clock=lambda: clock[0])
        first = client.get_overview()
        self.assertIs(first, client.get_overview())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "https://api.crypto.com/exchange/v1/public/get-tickers")
        self.assertNotIn("Authorization", calls[0][1])
        clock[0] += 16
        client.get_overview()
        self.assertEqual(len(calls), 2)

    def test_invalid_provider_response_is_rejected(self):
        for body in (b"not json", b'{"code":401,"result":{}}', b'{"code":0,"result":{"data":null}}'):
            client = CryptoComMarketOverview(opener=lambda *_args, body=body, **_kwargs: FakeResponse(body))
            with self.subTest(body=body), self.assertRaises(CryptoComMarketOverviewError):
                client.get_overview()


if __name__ == "__main__":
    unittest.main()
