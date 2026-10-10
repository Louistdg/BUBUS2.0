"""Bounded, cached, public historical observations for real charts."""
from __future__ import annotations

import hashlib
import json
import ssl
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bubus.store import SYMBOL

_MAX_CACHED_CANDLE_PAIRS = 2000


class CandleSource:
    def __init__(self, opener=urlopen):
        self.opener = opener
        self.cache = {}
        self._live_updates = {}
        self.lock = threading.RLock()

    def update_live_candle(self, symbol: str, timeframe: str, row: object,
                            received_at: float, payload_sha256: str) -> None:
        """Merge one verified public WebSocket candle into the bounded history cache."""
        if not SYMBOL.fullmatch(symbol) or timeframe not in {"5m", "15m", "1h", "4h", "1D"}:
            raise ValueError("Instrument ou période invalide.")
        if (not isinstance(row, Mapping) or type(row.get("t")) is not int or row["t"] <= 0
                or not isinstance(received_at, (int, float)) or isinstance(received_at, bool)
                or not time.time() - 30 <= received_at <= time.time() + 5
                or not isinstance(payload_sha256, str) or len(payload_sha256) != 64
                or any(char not in "0123456789abcdef" for char in payload_sha256)):
            raise ValueError("Candle live Crypto.com invalide.")
        try:
            values = {name: Decimal(str(row[name])) for name in ("o", "h", "l", "c", "v")}
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ValueError("Candle live Crypto.com invalide.") from None
        if (any(not value.is_finite() for value in values.values())
                or min(values[name] for name in ("o", "h", "l", "c")) <= 0
                or values["v"] < 0
                or values["h"] < max(values["o"], values["c"], values["l"])
                or values["l"] > min(values["o"], values["c"])):
            raise ValueError("Candle live Crypto.com invalide.")
        age = float(received_at) - row["t"] / 1000
        max_age = {"5m": 600, "15m": 1800, "1h": 7200, "4h": 28800, "1D": 172800}[timeframe]
        if not -60 <= age <= max_age:
            raise ValueError("Horodatage de candle live Crypto.com périmé ou futur.")
        normalized = {"timestamp": row["t"], **{name: str(value) for name, value in values.items()}}
        key = (symbol, timeframe)
        update = (normalized, float(received_at), payload_sha256)
        with self.lock:
            self._live_updates[key] = update
            cached = self.cache.get(key)
            if cached is not None:
                self.cache[key] = (time.monotonic(), self._merge_live(cached[1], update))

    @staticmethod
    def _merge_live(payload: dict, update: tuple[dict, float, str], *, request_started_at: float | None = None) -> dict:
        candle, received_at, event_digest = update
        if request_started_at is not None and received_at < request_started_at:
            return payload
        rows = {item["timestamp"]: dict(item) for item in payload["candles"]}
        latest_timestamp = max(rows, default=0)
        if candle["timestamp"] < latest_timestamp:
            return payload
        rows[candle["timestamp"]] = dict(candle)
        merged = dict(payload)
        merged["candles"] = [rows[stamp] for stamp in sorted(rows)[-120:]]
        material = json.dumps({"history": payload["payload_sha256"], "event": event_digest,
                               "candles": merged["candles"]}, sort_keys=True, separators=(",", ":"))
        merged["payload_sha256"] = hashlib.sha256(material.encode("utf-8")).hexdigest()
        merged["received_at"] = received_at
        return merged

    def get(self, symbol: str, timeframe: str):
        if not SYMBOL.fullmatch(symbol) or timeframe not in {"5m", "15m", "1h", "4h", "1D"}:
            raise ValueError("Instrument ou période invalide.")
        with self.lock:
            key = (symbol, timeframe)
            cached = self.cache.get(key)
            live_update = self._live_updates.get(key)
            cache_ttl = 25 if live_update is not None and time.time() - live_update[1] < 30 else 12
            if cached and time.monotonic() - cached[0] < cache_ttl:
                return cached[1]
        request_started_at = time.time()
        url = "https://api.crypto.com/exchange/v1/public/get-candlestick?" + urlencode(
            {"instrument_name": symbol, "timeframe": timeframe, "count": 120})
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "BUBUS2.0/2"})
        try:
            with self.opener(request, timeout=10, context=ssl.create_default_context()) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError()
            data = json.loads(raw, parse_float=Decimal,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            result = data.get("result")
            if data.get("code") != 0 or not isinstance(result, dict) or result.get("instrument_name") != symbol:
                raise ValueError()
            candles = []
            for row in result.get("data", [])[-120:]:
                values = {name: Decimal(str(row[name])) for name in ("o", "h", "l", "c", "v")}
                if any(not v.is_finite() for v in values.values()):
                    raise ValueError()
                if min(values[name] for name in ("o", "h", "l", "c")) <= 0 or values["v"] < 0:
                    raise ValueError()
                if values["h"] < max(values["o"], values["c"], values["l"]) or values["l"] > min(values["o"], values["c"]):
                    raise ValueError()
                if type(row["t"]) is not int or row["t"] <= 0:
                    raise ValueError()
                candles.append({"timestamp": row["t"], **{k: str(v) for k, v in values.items()}})
            candles.sort(key=lambda c: c["timestamp"])
            if len({c["timestamp"] for c in candles}) != len(candles):
                raise ValueError()
            payload = {"symbol": symbol, "timeframe": timeframe, "candles": candles,
                       "source": "Crypto.com Exchange", "payload_sha256": hashlib.sha256(raw).hexdigest(),
                       "received_at": time.time(), "orders_enabled": False}
        except Exception:
            raise ValueError("Historique Crypto.com indisponible ou invalide.") from None
        with self.lock:
            if len(self.cache) >= _MAX_CACHED_CANDLE_PAIRS:
                removed_key = next(iter(self.cache))
                self.cache.pop(removed_key)
                self._live_updates.pop(removed_key, None)
            update = self._live_updates.get(key)
            if update is not None:
                payload = self._merge_live(payload, update, request_started_at=request_started_at)
            self.cache[key] = (time.monotonic(), payload)
            return payload
