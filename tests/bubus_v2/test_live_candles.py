from __future__ import annotations

import hashlib
import json
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from bubus.market import CandleSource


class _Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *_args): pass
    def read(self, _limit): return json.dumps(self.payload).encode()


def _bar_timestamp():
    return int(time.time() * 1000) // 300_000 * 300_000


def _rest_payload(timestamp=None):
    timestamp = _bar_timestamp() if timestamp is None else timestamp
    return {"code": 0, "result": {"instrument_name": "BTC_USDT", "data": [
        {"t": timestamp, "o": "10", "h": "12", "l": "9", "c": "11", "v": "100"},
    ]}}


def _live_row(timestamp=None, *, close="12"):
    timestamp = _bar_timestamp() if timestamp is None else timestamp
    return {"t": timestamp, "o": "11", "h": close, "l": "10", "c": close, "v": "120"}


class LiveCandlestickCacheTests(unittest.TestCase):
    def test_cache_retains_history_beyond_the_first_128_market_pairs(self):
        calls = []

        def opener(request, **_kwargs):
            symbol = parse_qs(urlsplit(request.full_url).query)["instrument_name"][0]
            calls.append(symbol)
            payload = _rest_payload()
            payload["result"]["instrument_name"] = symbol
            return _Response(payload)

        source = CandleSource(opener=opener)
        symbols = [f"TKN{index:03d}_USD" for index in range(129)]
        for symbol in symbols:
            source.get(symbol, "5m")

        self.assertEqual(len(calls), 129)
        source.get(symbols[0], "5m")
        self.assertEqual(len(calls), 129)

    def test_live_update_during_history_fetch_is_merged_and_cached(self):
        source = None
        calls = []
        digest = "a" * 64
        base_timestamp = _bar_timestamp()

        def opener(_request, **_kwargs):
            calls.append(1)
            source.update_live_candle("BTC_USDT", "5m", _live_row(base_timestamp),
                                      time.time(), digest)
            return _Response(_rest_payload(base_timestamp - 300_000))

        source = CandleSource(opener=opener)
        result = source.get("BTC_USDT", "5m")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result["candles"]), 2)
        self.assertEqual(result["candles"][-1]["c"], "12")
        history_digest = hashlib.sha256(json.dumps(_rest_payload(base_timestamp - 300_000)).encode()).hexdigest()
        material = json.dumps({"history": history_digest, "event": digest,
                               "candles": result["candles"]}, sort_keys=True, separators=(",", ":"))
        self.assertEqual(result["payload_sha256"], hashlib.sha256(material.encode()).hexdigest())
        self.assertLessEqual(time.time() - result["received_at"], 2)
        source.get("BTC_USDT", "5m")
        self.assertEqual(len(calls), 1)

    def test_live_update_replaces_same_bar_and_advances_provenance(self):
        timestamp = _bar_timestamp()
        source = CandleSource(opener=lambda *_args, **_kwargs: _Response(_rest_payload(timestamp)))
        source.get("BTC_USDT", "5m")
        previous_digest = source.cache[("BTC_USDT", "5m")][1]["payload_sha256"]
        source.update_live_candle("BTC_USDT", "5m", _live_row(timestamp=timestamp),
                                  time.time(), "b" * 64)
        result = source.get("BTC_USDT", "5m")
        self.assertEqual(len(result["candles"]), 1)
        self.assertEqual(result["candles"][0]["c"], "12")
        self.assertNotEqual(result["payload_sha256"], previous_digest)

    def test_invalid_candlestick_is_rejected_before_cache_mutation(self):
        source = CandleSource()
        invalid = [
            ("../bad", "5m", _live_row(), "a" * 64),
            ("BTC_USDT", "5m", {**_live_row(), "h": "9"}, "a" * 64),
            ("BTC_USDT", "1m", _live_row(), "a" * 64),
            ("ETH_USDT", "5m", _live_row(), "bad"),
        ]
        for symbol, timeframe, row, digest in invalid:
            with self.subTest(symbol=symbol, timeframe=timeframe), self.assertRaises(ValueError):
                source.update_live_candle(symbol, timeframe, row, time.time(), digest)
        self.assertEqual(source.cache, {})
        self.assertEqual(source._live_updates, {})


if __name__ == "__main__":
    unittest.main()
