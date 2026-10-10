"""Explicitly started, read-only local Crypto.com portfolio observation worker."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable


class ReadOnlyPortfolioObserver:
    """Run a bounded collection callback until stopped; never starts on construction."""

    def __init__(self, collect: Callable[[], object], *, interval_seconds: float = 60) -> None:
        if not callable(collect):
            raise TypeError("A portfolio collection callback is required.")
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)) or interval_seconds <= 0:
            raise ValueError("Observer interval must be positive.")
        self._collect = collect
        self._interval_seconds = float(interval_seconds)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: str | None = None
        self._last_attempt_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error_code: str | None = None
        self._last_collection: dict[str, object] = {
            "portfolio_recorded": False, "valuation_recorded": False,
        }
        self._completed_cycles = 0
        self._worker_generation = 0

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._started_at = self._now()
            self._last_error_code = None
            self._worker_generation += 1
            self._thread = threading.Thread(target=self._run, name="cryptocom-readonly-observer", daemon=True)
            self._thread.start()
            return True

    def stop(self, *, timeout: float | None = 2) -> bool:
        if (timeout is not None and
                (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0)):
            raise ValueError("Observer stop timeout must be nonnegative.")
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return True
            self._stop_event.set()
        thread.join(timeout)
        return not thread.is_alive()

    def run_if_active(self, action: Callable[[], object]) -> bool:
        """Run a short persistence action atomically with respect to stop requests."""
        if not callable(action):
            raise TypeError("An observer action is required.")
        with self._lock:
            if (self._stop_event.is_set() or self._thread is None
                    or not self._thread.is_alive()):
                return False
            action()
            return True

    def state(self) -> dict[str, object]:
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            stopping = alive and self._stop_event.is_set()
            status = "STOPPING" if stopping else "RUNNING" if alive else "STOPPED"
            return {
                "status": status,
                "active": alive,
                "started_at": self._started_at,
                "last_attempt_at": self._last_attempt_at,
                "last_success_at": self._last_success_at,
                "last_error_code": self._last_error_code,
                "last_collection": dict(self._last_collection),
                "completed_cycles": self._completed_cycles,
                "worker_generation": self._worker_generation,
                "interval_seconds": self._interval_seconds,
                "authority_status": "LOCAL_READ_ONLY_OBSERVATION_ONLY",
                "usable_for_risk": False,
                "usable_for_execution": False,
                "orders_enabled": False,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                self._last_attempt_at = self._now()
            try:
                result = self._collect()
            except Exception:
                with self._lock:
                    self._last_error_code = "COLLECTION_FAILED"
                    self._last_collection = {"portfolio_recorded": False, "valuation_recorded": False}
                    self._completed_cycles += 1
            else:
                with self._lock:
                    self._last_success_at = self._now()
                    self._last_error_code = None
                    self._last_collection = self._safe_collection_result(result)
                    self._completed_cycles += 1
            delay = self._next_delay(datetime.now(timezone.utc), self._interval_seconds)
            if self._stop_event.wait(delay):
                break

    @staticmethod
    def _next_delay(now: datetime, interval_seconds: float) -> float:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Observer scheduler time must include a timezone.")
        current = now.astimezone(timezone.utc)
        next_midnight = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        seconds_to_midnight = (next_midnight - current).total_seconds()
        return min(interval_seconds, max(0.01, seconds_to_midnight))

    @staticmethod
    def _safe_collection_result(result: object) -> dict[str, object]:
        if not isinstance(result, dict):
            return {"portfolio_recorded": True, "valuation_recorded": False}
        return {
            "portfolio_recorded": result.get("portfolio_recorded") is True,
            "valuation_recorded": result.get("valuation_recorded") is True,
            "valuation_error_code": (result.get("valuation_error_code")
                if result.get("valuation_error_code") in {"MARKET_DATA_UNAVAILABLE", "VALUATION_REJECTED"}
                else None),
        }


__all__ = ["ReadOnlyPortfolioObserver"]
