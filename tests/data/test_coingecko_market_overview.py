from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from invest_ai.data.coingecko_market_overview import CoinGeckoMarketOverview, CoinGeckoMarketOverviewError


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, amount=-1):
        return self.body[:amount]


class CoinGeckoMarketOverviewTests(unittest.TestCase):
    def test_parses_real_market_fields_and_includes_required_attribution(self):
        data = [
            {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "current_price": 60000.1,
             "market_cap": 1000000, "market_cap_rank": 1, "total_volume": 500,
             "price_change_percentage_24h": -0.42, "last_updated": "2026-10-09T20:00:00Z"},
            {"id": "bad coin", "symbol": "bad", "name": "Bad", "current_price": 1,
             "market_cap": 1, "market_cap_rank": 2, "total_volume": 1,
             "price_change_percentage_24h": 0, "last_updated": "2026-10-09T20:00:00Z"},
            {"id": "empty", "symbol": "emp", "name": "Empty", "current_price": None,
             "market_cap": 1, "market_cap_rank": 3, "total_volume": 1,
             "price_change_percentage_24h": 0, "last_updated": "2026-10-09T20:00:00Z"},
        ]
        client = CoinGeckoMarketOverview(
            opener=lambda request, timeout: FakeResponse(json.dumps(data).encode()),
            clock=lambda: 1.0,
            utc_clock=lambda: datetime(2026, 10, 9, tzinfo=timezone.utc),
        )
        result = client.get_overview()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["markets"][0]["id"], "bitcoin")
        self.assertEqual(result["markets"][0]["symbol"], "BTC")
        self.assertEqual(result["markets"][0]["price_usd"], "60000.1")
        self.assertEqual(result["markets"][0]["change_24h_pct"], "-0.42")
        self.assertEqual(result["attribution_text"], "Données fournies par CoinGecko")
        self.assertFalse(result["persistent"])
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(len(result["markets"][0]["payload_sha256"]), 64)

    def test_uses_only_fixed_keyless_endpoint_and_caches_for_60_seconds(self):
        calls = []
        clock = [2.0]
        body = json.dumps([{
            "id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "current_price": 2,
            "market_cap": 2, "market_cap_rank": 1, "total_volume": 1,
            "price_change_percentage_24h": 0, "last_updated": "2026-10-09T20:00:00Z",
        }]).encode()
        def opener(request, timeout):
            calls.append((request.full_url, request.headers, timeout))
            return FakeResponse(body)
        client = CoinGeckoMarketOverview(opener=opener, clock=lambda: clock[0])
        first = client.get_overview()
        self.assertIs(first, client.get_overview())
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].startswith("https://api.coingecko.com/api/v3/coins/markets?"))
        self.assertNotIn("X-cg-demo-api-key", calls[0][1])
        self.assertEqual(first["refresh_after_seconds"], 60)
        clock[0] += 61
        client.get_overview()
        self.assertEqual(len(calls), 2)

    def test_requests_and_returns_the_real_provider_maximum_of_250_coins(self):
        calls = []
        rows = [{
            "id": f"coin-{rank}", "symbol": f"c{rank}", "name": f"Coin {rank}",
            "current_price": rank, "market_cap": 1000 - rank,
            "market_cap_rank": rank, "total_volume": 10,
            "price_change_percentage_24h": 0,
            "last_updated": "2026-10-09T20:00:00Z",
        } for rank in range(1, 251)]
        body = json.dumps(rows).encode()

        def opener(request, timeout):
            calls.append(request.full_url)
            return FakeResponse(body)

        result = CoinGeckoMarketOverview(opener=opener).get_overview()
        self.assertIn("per_page=250", calls[0])
        self.assertEqual(result["count"], 250)
        self.assertEqual(result["markets"][-1]["market_cap_rank"], 250)

    def test_rejects_malformed_and_non_list_responses(self):
        for body in (b"not json", b'{"error":"rate limit"}', b"null"):
            client = CoinGeckoMarketOverview(opener=lambda *_args, body=body, **_kwargs: FakeResponse(body))
            with self.subTest(body=body), self.assertRaises(CoinGeckoMarketOverviewError):
                client.get_overview()


if __name__ == "__main__":
    unittest.main()
