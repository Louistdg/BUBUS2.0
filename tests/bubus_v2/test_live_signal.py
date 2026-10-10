from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bubus.live_signal import LiveSignalError, observe_cryptocom_breakout
from invest_ai.data.cryptocom_market_readonly import CryptoComMarketTick


BASE = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def market_inputs():
    start = BASE - timedelta(minutes=20)
    rows = []
    values = (
        ("100", "100.10", "99.90", "100", "1000"),
        ("100.20", "100.30", "100.10", "100.20", "950"),
        ("100.50", "100.60", "100.40", "100.50", "1020"),
        ("100.50", "105.10", "100.40", "105", "3500"),
    )
    for index, (open_, high, low, close, volume) in enumerate(values):
        rows.append({"timestamp": int((start + timedelta(minutes=5 * index)).timestamp() * 1000),
                     "o": open_, "h": high, "l": low, "c": close, "v": volume})
    quote = CryptoComMarketTick(
        symbol="BTC_USDT", price=Decimal("105"), best_bid=Decimal("104.99"),
        best_ask=Decimal("105.01"), quote_status="COMPLETE",
        provider_timestamp=BASE + timedelta(seconds=1), received_at=BASE + timedelta(seconds=1.5),
        currency="USDT", exchange="Crypto.com Exchange", instrument_type="Spot",
        source="Crypto.com Exchange public ticker WebSocket", payload_sha256="d" * 64,
        best_bid_size=Decimal("120"), best_ask_size=Decimal("100"))
    payload = {"symbol": "BTC_USDT", "timeframe": "5m", "candles": rows,
               "source": "Crypto.com Exchange", "payload_sha256": "a" * 64,
               "received_at": (BASE + timedelta(seconds=1)).timestamp()}
    rules = {"base_currency": "BTC", "quote_currency": "USDT", "tradable": "true",
             "price_tick_size": "0.01", "quantity_tick_size": "0.00001"}
    return payload, quote, rules


class LiveBreakoutObservationTests(unittest.TestCase):
    def test_builds_observation_only_signal_from_closed_live_crypto_bars(self):
        candles, tick, rules = market_inputs()
        signal = observe_cryptocom_breakout("BTC_USDT", "5m", candles, tick, rules,
                                            as_of=BASE + timedelta(seconds=2))
        self.assertEqual(signal.status.value, "SIGNAL_OBSERVED")
        self.assertEqual(signal.direction, "UP_BREAKOUT")
        self.assertEqual(signal.symbol, "BTC_USDT")
        self.assertEqual(len(signal.evidence_refs), 5)
        self.assertEqual(signal.authority_status, "OBSERVATION_ONLY")
        self.assertFalse(signal.execution_allowed)
        self.assertFalse(hasattr(signal, "quantity"))

    def test_rejects_an_incomplete_quote_and_mismatched_market_payload(self):
        candles, tick, rules = market_inputs()
        incomplete = replace(tick, best_ask=None)
        with self.assertRaises(LiveSignalError):
            observe_cryptocom_breakout("BTC_USDT", "5m", candles, incomplete, rules,
                                       as_of=BASE + timedelta(seconds=2))
        wrong_payload = dict(candles, symbol="ETH_USDT")
        with self.assertRaises(LiveSignalError):
            observe_cryptocom_breakout("BTC_USDT", "5m", wrong_payload, tick, rules,
                                       as_of=BASE + timedelta(seconds=2))

    def test_open_candle_is_excluded_and_cannot_create_a_signal(self):
        candles, tick, rules = market_inputs()
        last = dict(candles["candles"][-1])
        last["timestamp"] = int(BASE.timestamp() * 1000)
        candles = dict(candles, candles=candles["candles"][:-1] + [last])
        signal = observe_cryptocom_breakout("BTC_USDT", "5m", candles, tick, rules,
                                            as_of=BASE + timedelta(seconds=2))
        self.assertEqual(signal.status.value, "BLOCKED")
        self.assertEqual(signal.reason_codes, ("INSUFFICIENT_HISTORY",))


if __name__ == "__main__":
    unittest.main()
