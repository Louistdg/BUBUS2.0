from __future__ import annotations

import json
import time
import unittest
from decimal import Decimal
from urllib.parse import urlsplit

from invest_ai.data.cryptocom_instruments import CryptoComInstrumentCatalog, CryptoComInstrumentCatalogError


class FakeResponse:
    def __init__(self, body: bytes): self.body = body
    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def read(self, amount=-1): return self.body[:amount]


class CryptoComInstrumentCatalogTests(unittest.TestCase):
    def _order_catalog(self, *, ticker_age_seconds=0, ticker_bid="0.9999", calls=None):
        rows = [
            {"symbol": "BTC_USDT", "inst_type": "CCY_PAIR", "base_ccy": "BTC", "quote_ccy": "USDT",
             "price_tick_size": "0.01", "qty_tick_size": "0.00001", "tradable": True},
            {"symbol": "USDT_USD", "inst_type": "CCY_PAIR", "base_ccy": "USDT", "quote_ccy": "USD",
             "price_tick_size": "0.00001", "qty_tick_size": "0.1", "tradable": True},
            {"symbol": "BTC_USD", "inst_type": "CCY_PAIR", "base_ccy": "BTC", "quote_ccy": "USD",
             "price_tick_size": "0.01", "qty_tick_size": "0.00001", "tradable": True},
        ]
        def opener(request, timeout):
            if calls is not None:
                calls.append(request.full_url)
            if urlsplit(request.full_url).path.endswith("/get-instruments"):
                payload = {"code": 0, "result": {"data": rows}}
            else:
                symbol = urlsplit(request.full_url).query.split("=", 1)[1]
                bid, ask = ((ticker_bid, "1.0001") if symbol == "USDT_USD"
                            else ("60000", "60000.02"))
                payload = {"code": 0, "result": {"data": [{
                    "i": symbol, "b": bid, "k": ask,
                    "t": int((time.time() - ticker_age_seconds) * 1000),
                }]}}
            return FakeResponse(json.dumps(payload).encode())
        return CryptoComInstrumentCatalog(opener=opener)

    def test_order_verifier_requires_a_current_tradable_pair_and_exact_price_and_quantity_steps(self):
        from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder, CryptoComSpotOrderError

        catalog = self._order_catalog()
        valid = CryptoComSpotLimitOrder("fa14b16b-6b53-4716-8e78-253ad1cfe600", "BTC_USDT", "BUY",
                                        Decimal("60000.01"), Decimal("0.00011"))
        catalog.assert_spot_limit_order(valid)
        too_small = CryptoComSpotLimitOrder(valid.client_oid, "BTC_USDT", "BUY",
                                            Decimal("60000.01"), Decimal("0.00001"))
        with self.assertRaisesRegex(CryptoComSpotOrderError, "at least 1 USD"):
            catalog.assert_spot_limit_order(too_small)
        buy_limit_above_ask = CryptoComSpotLimitOrder(
            valid.client_oid, "BTC_USDT", "BUY", Decimal("120000"), Decimal("0.00001"))
        with self.assertRaisesRegex(CryptoComSpotOrderError, "at least 1 USD"):
            catalog.assert_spot_limit_order(buy_limit_above_ask)
        invalid = (
            CryptoComSpotLimitOrder(valid.client_oid, "BTC_USDT", "BUY", Decimal("60000.001"), valid.quantity),
            CryptoComSpotLimitOrder(valid.client_oid, "BTC_USDT", "BUY", valid.price, Decimal("0.000111")),
            CryptoComSpotLimitOrder(valid.client_oid, "ETH_USDT", "BUY", valid.price, valid.quantity),
        )
        for order in invalid:
            with self.subTest(order=order), self.assertRaises(CryptoComSpotOrderError):
                catalog.assert_spot_limit_order(order)

    def test_quote_conversion_must_be_fresh_and_supported_before_minimum_is_accepted(self):
        from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder, CryptoComSpotOrderError

        order = CryptoComSpotLimitOrder("fa14b16b-6b53-4716-8e78-253ad1cfe600", "BTC_USDT", "BUY",
                                        Decimal("60000"), Decimal("0.00002"))
        with self.assertRaisesRegex(CryptoComSpotOrderError, "fresh Crypto.com"):
            self._order_catalog(ticker_age_seconds=6).assert_spot_limit_order(order)

        calls = []
        catalog = self._order_catalog(calls=calls)
        usd_order = CryptoComSpotLimitOrder(order.client_oid, "BTC_USD", "BUY",
                                            Decimal("60000"), Decimal("0.00002"))
        catalog.assert_spot_limit_order(usd_order)
        ticker_calls = [call for call in calls if "get-tickers" in call]
        self.assertEqual(len(ticker_calls), 1)
        self.assertIn("instrument_name=BTC_USD", ticker_calls[0])

    def test_returns_only_tradable_spot_pairs_with_positive_exchange_increments(self):
        rows = [
            {"symbol": "BTC_USDT", "inst_type": "CCY_PAIR", "base_ccy": "BTC", "quote_ccy": "USDT",
             "price_tick_size": "0.01", "qty_tick_size": "0.00001", "tradable": True},
            {"symbol": "BTCUSD-PERP", "inst_type": "PERPETUAL_SWAP", "tradable": True},
            {"symbol": "ETH_USDT", "inst_type": "CCY_PAIR", "base_ccy": "ETH", "quote_ccy": "USDT",
             "price_tick_size": "0", "qty_tick_size": "0.01", "tradable": True},
            {"symbol": "XRP_USDT", "inst_type": "CCY_PAIR", "base_ccy": "XRP", "quote_ccy": "USDT",
             "price_tick_size": "0.0001", "qty_tick_size": "1", "tradable": False},
            {"symbol": "ETH_USDT", "inst_type": "CCY_PAIR", "base_ccy": "BTC", "quote_ccy": "USDT",
             "price_tick_size": "0.01", "qty_tick_size": "0.01", "tradable": True},
        ]
        calls = []
        def opener(request, timeout):
            calls.append((request.full_url, timeout, dict(request.header_items())))
            return FakeResponse(json.dumps({"code": 0, "result": {"data": rows}}).encode())
        now = [0.0]
        catalog = CryptoComInstrumentCatalog(opener=opener, clock=lambda: now[0])
        first = catalog.get_spot_catalog()
        self.assertEqual(first, {"BTC_USDT": {"base_currency": "BTC", "quote_currency": "USDT",
            "price_tick_size": "0.01", "quantity_tick_size": "0.00001", "tradable": "true"}})
        self.assertIs(first, catalog.get_spot_catalog())
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith("/public/get-instruments"))
        self.assertNotIn("Authorization", calls[0][2])
        now[0] = 61
        catalog.get_spot_catalog()
        self.assertEqual(len(calls), 2)

    def test_invalid_catalog_is_rejected(self):
        for body in (b"not json", b'{"code":401,"result":{}}', b'{"code":0,"result":{"data":null}}'):
            catalog = CryptoComInstrumentCatalog(opener=lambda *_args, body=body, **_kwargs: FakeResponse(body))
            with self.subTest(body=body), self.assertRaises(CryptoComInstrumentCatalogError):
                catalog.get_spot_catalog()


if __name__ == "__main__":
    unittest.main()
