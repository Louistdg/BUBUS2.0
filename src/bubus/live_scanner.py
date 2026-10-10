"""Rate-limited continuous observations across the connected Crypto.com Spot universe."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Mapping

from bubus.live_signal import LiveSignalError, observe_cryptocom_breakout


class LiveSignalScanner:
    """Walk connected Spot pairs and retain their latest real-data observation.

    Candle-history requests run in bounded batches. A minimum 100 ms pause
    between batches and at most eight workers cap this public method below 80
    requests/second. The scanner only creates observations; it cannot size or
    send orders and has no account credentials.
    """

    MAX_OBSERVATION_AGE_SECONDS = 30

    def __init__(self, market_stream, instrument_catalog, candle_source, *,
                 interval_seconds: float = 0.1, max_workers: int = 8, clock=None) -> None:
        if not isinstance(interval_seconds, (int, float)) or not 0.1 <= interval_seconds <= 10:
            raise ValueError("Scan interval is outside the permitted range.")
        if type(max_workers) is not int or not 1 <= max_workers <= 8:
            raise ValueError("Scan concurrency is outside the permitted range.")
        self._stream = market_stream
        self._catalog = instrument_catalog
        self._candles = candle_source
        self._interval = float(interval_seconds)
        self._max_workers = max_workers
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._timeframe = "5m"
        self._symbols: tuple[str, ...] = ()
        self._results: dict[str, dict[str, object]] = {}
        self._scanned_count = 0
        self._scan_pass = 0
        self._current_symbol: str | None = None
        self._active_symbols: set[str] = set()
        self._last_completed_at: str | None = None
        self._status = "STOPPED"

    def start(self, symbols: tuple[str, ...] | list[str], *, timeframe: str = "5m") -> dict[str, object]:
        if timeframe not in {"5m", "15m", "1h", "4h", "1D"}:
            raise ValueError("Unsupported live scanner timeframe.")
        if not isinstance(symbols, (tuple, list)) or not 1 <= len(symbols) <= 2000:
            raise ValueError("The scanner requires a bounded, non-empty Spot universe.")
        cleaned = tuple(dict.fromkeys(symbol for symbol in symbols if isinstance(symbol, str)))
        if not cleaned or len(cleaned) > 2000:
            raise ValueError("The scanner requires a bounded, non-empty Spot universe.")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if cleaned == self._symbols and timeframe == self._timeframe:
                    return self.state()
                raise RuntimeError("Stop the current live scan before changing its universe.")
            self._symbols = cleaned
            self._timeframe = timeframe
            self._results = {}
            self._scanned_count = 0
            self._scan_pass = 0
            self._current_symbol = None
            self._active_symbols.clear()
            self._last_completed_at = None
            self._status = "RUNNING"
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="bubus-live-signal-scan")
            self._thread.start()
            return self.state()

    def stop(self) -> dict[str, object]:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=12)
        with self._lock:
            if thread is None or not thread.is_alive():
                self._thread = None
                self._current_symbol = None
                self._status = "STOPPED"
            return self.state()

    def scan_symbol(self, symbol: str) -> dict[str, object]:
        """Evaluate one pair; public for deterministic route/tests and manual refresh."""
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Scanner clock must return a timezone-aware datetime.")
        tick = self._stream.get_tick(symbol)
        if tick is None:
            return self._blocked(symbol, observed_at, "FRESH_TICK_UNAVAILABLE")
        if not self._fresh_complete_tick(tick, observed_at):
            return self._blocked(symbol, observed_at, "FRESH_COMPLETE_QUOTE_UNAVAILABLE")
        try:
            rules = self._catalog.get_spot_catalog().get(symbol)
            if rules is None:
                return self._blocked(symbol, observed_at, "CURRENT_SPOT_RULES_UNAVAILABLE")
            candles = self._candles.get(symbol, self._timeframe)
            observed_at = self._clock()
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                raise ValueError("Scanner clock must return a timezone-aware datetime.")
            if not self._fresh_complete_tick(tick, observed_at):
                return self._blocked(symbol, observed_at, "FRESH_COMPLETE_QUOTE_UNAVAILABLE")
            result = observe_cryptocom_breakout(
                symbol, self._timeframe, candles, tick, rules, as_of=observed_at)
            return {
                "symbol": symbol, "status": result.status.value, "direction": result.direction,
                "observed_at": result.observed_at.isoformat().replace("+00:00", "Z"),
                "reason_codes": list(result.reason_codes),
                "metrics": [{"name": name, "value": value} for name, value in result.metrics],
                "evidence_refs": list(result.evidence_refs),
                "authority_status": result.authority_status,
                "execution_allowed": False, "orders_enabled": False,
            }
        except LiveSignalError as exc:
            result = self._blocked(symbol, observed_at, "MARKET_EVIDENCE_BLOCKED")
            result["detail"] = str(exc)
            return result
        except (ValueError, TypeError, KeyError, OSError):
            return self._blocked(symbol, observed_at, "MARKET_SOURCE_UNAVAILABLE")

    @staticmethod
    def _fresh_complete_tick(tick, observed_at: datetime) -> bool:
        try:
            if (tick.received_at.tzinfo is None or tick.received_at.utcoffset() is None
                    or tick.provider_timestamp.tzinfo is None or tick.provider_timestamp.utcoffset() is None):
                return False
            cutoff = observed_at.astimezone(timezone.utc)
            received_at = tick.received_at.astimezone(timezone.utc)
            provider_at = tick.provider_timestamp.astimezone(timezone.utc)
            quote_age = (cutoff - received_at).total_seconds()
            provider_age = (cutoff - provider_at).total_seconds()
            return (0 <= quote_age <= 5 and 0 <= provider_age <= 5
                    and tick.quote_status == "COMPLETE" and tick.best_bid_size is not None
                    and tick.best_ask_size is not None)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False

    def state(self, *, result_limit: int | None = None) -> dict[str, object]:
        if result_limit is not None and (type(result_limit) is not int or not 1 <= result_limit <= 2000):
            raise ValueError("Invalid live scanner result limit.")
        with self._lock:
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                now = None
            else:
                now = now.astimezone(timezone.utc)
            results = []
            for stored in self._results.values():
                row = dict(stored)
                age = None
                if now is not None:
                    try:
                        observed = datetime.fromisoformat(
                            str(row.get("observed_at", "")).replace("Z", "+00:00"))
                        if observed.tzinfo is not None and observed.utcoffset() is not None:
                            age = (now - observed.astimezone(timezone.utc)).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        pass
                row["age_seconds"] = round(age, 1) if age is not None else None
                row["fresh"] = age is not None and 0 <= age <= self.MAX_OBSERVATION_AGE_SECONDS
                results.append(row)
            results.sort(key=lambda item: item["symbol"])
            status_counts: dict[str, int] = {}
            fresh_status_counts: dict[str, int] = {}
            for row in results:
                status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
                if row["fresh"]:
                    fresh_status_counts[row["status"]] = fresh_status_counts.get(row["status"], 0) + 1
            signal_count = status_counts.get("SIGNAL_OBSERVED", 0)
            fresh_signal_count = sum(row["status"] == "SIGNAL_OBSERVED" and row["fresh"] for row in results)
            fresh_observation_count = sum(row["fresh"] for row in results)
            visible_results = results[:result_limit] if result_limit is not None else results
            return {
                "status": self._status, "timeframe": self._timeframe,
                "symbols_total": len(self._symbols), "scanned_count": self._scanned_count,
                "scan_pass": self._scan_pass, "current_symbol": self._current_symbol,
                "active_scans": len(self._active_symbols), "max_concurrency": self._max_workers,
                "last_completed_at": self._last_completed_at,
                "signals_count": signal_count, "status_counts": status_counts,
                "freshness_window_seconds": self.MAX_OBSERVATION_AGE_SECONDS,
                "fresh_observations_count": fresh_observation_count,
                "stale_observations_count": len(results) - fresh_observation_count,
                "fresh_status_counts": fresh_status_counts,
                "fresh_signals_count": fresh_signal_count,
                "stale_signals_count": signal_count - fresh_signal_count,
                "results": [dict(row) for row in visible_results],
                "orders_enabled": False, "execution_allowed": False,
            }

    def _run(self) -> None:
        try:
            with ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="bubus-scan") as pool:
                while not self._stop.is_set():
                    with self._lock:
                        symbols = self._symbols
                    for offset in range(0, len(symbols), self._max_workers):
                        if self._stop.is_set():
                            break
                        batch = symbols[offset:offset + self._max_workers]
                        with self._lock:
                            self._active_symbols.update(batch)
                            self._current_symbol = batch[0] if batch else None
                        futures = {pool.submit(self.scan_symbol, symbol): symbol for symbol in batch}
                        for future in as_completed(futures):
                            symbol = futures[future]
                            try:
                                result = future.result()
                            except Exception:
                                result = self._blocked(symbol, self._clock(), "MARKET_SOURCE_UNAVAILABLE")
                            with self._lock:
                                self._results[symbol] = result
                                self._scanned_count += 1
                                self._active_symbols.discard(symbol)
                                self._current_symbol = next(iter(self._active_symbols), None)
                            if self._stop.is_set():
                                for pending in futures:
                                    pending.cancel()
                        if self._stop.wait(self._interval):
                            break
                    if self._stop.is_set():
                        break
                    with self._lock:
                        self._scan_pass += 1
                        self._scanned_count = 0
                        self._current_symbol = None
                        self._last_completed_at = self._clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        finally:
            with self._lock:
                self._active_symbols.clear()
                self._current_symbol = None
                self._status = "STOPPED"
                self._thread = None

    @staticmethod
    def _blocked(symbol: str, observed_at: datetime, code: str) -> dict[str, object]:
        return {
            "symbol": symbol, "status": "BLOCKED", "direction": None,
            "observed_at": observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "reason_codes": [code], "metrics": [], "evidence_refs": [],
            "authority_status": "OBSERVATION_ONLY", "execution_allowed": False,
            "orders_enabled": False,
        }


__all__ = ["LiveSignalScanner"]
