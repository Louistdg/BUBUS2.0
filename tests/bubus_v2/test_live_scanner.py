from __future__ import annotations

import time
import threading
import unittest
from dataclasses import replace
from datetime import timedelta

from bubus.live_scanner import LiveSignalScanner
from tests.bubus_v2.test_live_signal import BASE, market_inputs


class _Stream:
    def __init__(self, tick):
        self.tick = tick
    def get_tick(self, symbol):
        return self.tick if self.tick is not None and self.tick.symbol == symbol else None


class _Catalog:
    def __init__(self, rules):
        self.rules = rules
    def get_spot_catalog(self):
        return {"BTC_USDT": self.rules}


class _Candles:
    def __init__(self, payload):
        self.payload = payload
    def get(self, symbol, timeframe):
        if symbol != "BTC_USDT" or timeframe != "5m":
            raise ValueError("unexpected market request")
        return self.payload


class LiveSignalScannerTests(unittest.TestCase):
    def setUp(self):
        self.payload, self.tick, self.rules = market_inputs()
        self.now = BASE + timedelta(seconds=2)

    def make_scanner(self, *, include_tick=True):
        return LiveSignalScanner(_Stream(self.tick if include_tick else None),
            _Catalog(self.rules), _Candles(self.payload), interval_seconds=0.1,
            clock=lambda: self.now)

    def test_continuous_scan_retains_current_signal_without_execution_authority(self):
        scanner = self.make_scanner()
        started = scanner.start(["BTC_USDT"])
        self.assertEqual(started["status"], "RUNNING")
        deadline = time.monotonic() + 2
        state = scanner.state()
        while not state["results"] and time.monotonic() < deadline:
            time.sleep(0.01)
            state = scanner.state()
        try:
            self.assertEqual(state["results"][0]["status"], "SIGNAL_OBSERVED")
            self.assertEqual(state["signals_count"], 1)
            self.assertFalse(state["orders_enabled"])
            self.assertFalse(state["execution_allowed"])
            self.assertNotIn("quantity", state["results"][0])
        finally:
            stopped = scanner.stop()
        self.assertEqual(stopped["status"], "STOPPED")

    def test_scanner_timestamps_evaluation_after_history_fetch(self):
        payload = dict(self.payload, received_at=(BASE + timedelta(seconds=1.75)).timestamp())
        ticks = iter((BASE + timedelta(seconds=1.5), self.now))
        scanner = LiveSignalScanner(_Stream(self.tick), _Catalog(self.rules), _Candles(payload),
            interval_seconds=0.1, clock=lambda: next(ticks))
        result = scanner.scan_symbol("BTC_USDT")
        self.assertEqual(result["status"], "SIGNAL_OBSERVED")
        self.assertEqual(result["observed_at"], self.now.isoformat().replace("+00:00", "Z"))

    def test_missing_live_quote_is_retained_as_blocked_evidence(self):
        scanner = self.make_scanner(include_tick=False)
        result = scanner.scan_symbol("BTC_USDT")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["FRESH_TICK_UNAVAILABLE"])
        self.assertFalse(result["orders_enabled"])

    def test_state_excludes_old_signals_from_fresh_signal_count(self):
        scanner = self.make_scanner()
        scanner._results["BTC_USDT"] = scanner.scan_symbol("BTC_USDT")
        fresh = scanner.state()
        self.assertEqual(fresh["fresh_signals_count"], 1)
        self.assertEqual(fresh["stale_signals_count"], 0)
        self.assertEqual(fresh["fresh_observations_count"], 1)
        self.assertEqual(fresh["fresh_status_counts"], {"SIGNAL_OBSERVED": 1})
        self.assertTrue(fresh["results"][0]["fresh"])
        self.now += timedelta(seconds=31)
        stale = scanner.state()
        self.assertEqual(stale["signals_count"], 1)
        self.assertEqual(stale["fresh_signals_count"], 0)
        self.assertEqual(stale["stale_signals_count"], 1)
        self.assertEqual(stale["fresh_observations_count"], 0)
        self.assertEqual(stale["stale_observations_count"], 1)
        self.assertEqual(stale["fresh_status_counts"], {})
        self.assertFalse(stale["results"][0]["fresh"])
        self.assertFalse(stale["orders_enabled"])

    def test_result_limit_does_not_change_freshness_counts(self):
        scanner = self.make_scanner()
        first = scanner.scan_symbol("BTC_USDT")
        scanner._results["BTC_USDT"] = first
        scanner._results["ETH_USDT"] = {**first, "symbol": "ETH_USDT"}
        state = scanner.state(result_limit=1)
        self.assertEqual(len(state["results"]), 1)
        self.assertEqual(state["signals_count"], 2)
        self.assertEqual(state["fresh_signals_count"], 2)
        self.assertEqual(state["fresh_status_counts"], {"SIGNAL_OBSERVED": 2})

    def test_stale_quote_is_rejected_before_requesting_candles(self):
        stale = replace(self.tick, received_at=BASE - timedelta(seconds=10))
        class NoCandleRequest:
            def get(self, *_args):
                raise AssertionError("stale quotes must not trigger history requests")
        scanner = LiveSignalScanner(_Stream(stale), _Catalog(self.rules), NoCandleRequest(),
            interval_seconds=0.1, clock=lambda: self.now)
        result = scanner.scan_symbol("BTC_USDT")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["FRESH_COMPLETE_QUOTE_UNAVAILABLE"])

    def test_scanner_universe_is_bounded_and_deduplicated(self):
        scanner = self.make_scanner()
        with self.assertRaises(ValueError):
            scanner.start(["BTC_USDT"] * 2001)
        state = scanner.start(["BTC_USDT", "BTC_USDT"])
        self.assertEqual(state["symbols_total"], 1)
        scanner.stop()

    def test_full_market_scan_uses_bounded_parallel_workers(self):
        started_together = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        active = 0
        peak = 0

        class ParallelScanner(LiveSignalScanner):
            def scan_symbol(self, symbol):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                    if peak == 3:
                        started_together.set()
                release.wait(2)
                with guard:
                    active -= 1
                return self._blocked(symbol, self._clock(), "TEST_OBSERVATION")

        scanner = ParallelScanner(_Stream(self.tick), _Catalog(self.rules), _Candles(self.payload),
            interval_seconds=0.1, max_workers=3, clock=lambda: self.now)
        try:
            with self.assertRaises(ValueError):
                ParallelScanner(_Stream(self.tick), _Catalog(self.rules), _Candles(self.payload), max_workers=9)
            scanner.start([f"PAIR{index}_USD" for index in range(9)])
            self.assertTrue(started_together.wait(1), "three independent pair scans should overlap")
            release.set()
            deadline = time.monotonic() + 2
            while scanner.state()["scan_pass"] < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            state = scanner.state()
            self.assertEqual(peak, 3)
            self.assertEqual(state["max_concurrency"], 3)
            self.assertEqual(len(state["results"]), 9)
            self.assertGreaterEqual(state["scan_pass"], 1)
            self.assertFalse(state["orders_enabled"])
        finally:
            release.set()
            scanner.stop()


if __name__ == "__main__":
    unittest.main()
