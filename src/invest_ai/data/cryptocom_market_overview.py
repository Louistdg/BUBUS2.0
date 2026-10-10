"""Ephemeral bulk spot-market overview from Crypto.com Exchange public REST."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Protocol
from urllib.request import Request, urlopen


_TICKERS_URL = "https://api.crypto.com/exchange/v1/public/get-tickers"
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,31}_[A-Z0-9]{2,12}\Z")
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_MARKETS = 2000
_CACHE_SECONDS = 15


class CryptoComMarketOverviewError(ValueError):
    """Safe error for the public bulk ticker request."""


class _Response(Protocol):
    def read(self, amount: int = -1) -> bytes: ...
    def __enter__(self) -> "_Response": ...
    def __exit__(self, *_args: object) -> object: ...


Opener = Callable[..., _Response]


class CryptoComMarketOverview:
    """Fetch many authentic spot tickers without credentials or disk storage."""

    def __init__(self, *, opener: Opener | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 utc_clock: Callable[[], datetime] | None = None) -> None:
        self._opener = opener or urlopen
        self._clock = clock
        self._utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._cache_until = 0.0
        self._cached: dict[str, object] | None = None

    def get_overview(self) -> dict[str, object]:
        with self._lock:
            now = self._clock()
            if self._cached is not None and now < self._cache_until:
                return self._cached
            request = Request(
                _TICKERS_URL,
                headers={"Accept": "application/json", "User-Agent": "INVEST-AI-read-only-market/1"},
                method="GET",
            )
            try:
                with self._opener(request, timeout=10) as response:
                    raw = response.read(_MAX_RESPONSE_BYTES + 1)
            except Exception:
                raise CryptoComMarketOverviewError(
                    "Crypto.com Exchange ne répond pas; aucun cours de marché n’a été conservé."
                ) from None
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise CryptoComMarketOverviewError("La réponse publique de Crypto.com dépasse la taille autorisée.")
            try:
                payload = json.loads(raw, parse_float=Decimal, parse_int=int,
                                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, TypeError):
                raise CryptoComMarketOverviewError("Réponse de marché Crypto.com invalide.") from None
            if not isinstance(payload, dict):
                raise CryptoComMarketOverviewError("Réponse de marché Crypto.com invalide.")
            rows = payload.get("result", {}).get("data")
            if payload.get("code") != 0 or not isinstance(rows, list):
                raise CryptoComMarketOverviewError("Crypto.com n’a pas fourni la liste des cotations.")

            markets = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = row.get("i")
                if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
                    continue  # Spot CCY pairs only; discard derivatives and index symbols.
                try:
                    price = self._decimal(row.get("a"), positive=True)
                    best_bid = self._optional_decimal(row.get("b"))
                    best_ask = self._optional_decimal(row.get("k"))
                    volume_usd = self._decimal(row.get("vv"), positive=False)
                    change = self._decimal(row.get("c"), positive=False)
                    if volume_usd < 0:
                        continue
                    timestamp = row.get("t")
                    if isinstance(timestamp, str) and timestamp.isdigit():
                        timestamp = int(timestamp)
                    if type(timestamp) is not int or timestamp <= 0:
                        continue
                    provider_time = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
                except (InvalidOperation, TypeError, ValueError, OverflowError, OSError):
                    continue
                quote_currency = symbol.rsplit("_", 1)[1]
                quote_status = "INCOMPLETE"
                if best_bid is not None and best_ask is not None:
                    quote_status = "CROSSED" if best_bid > best_ask else "COMPLETE"
                markets.append({
                    "symbol": symbol,
                    "price": str(price),
                    "last_trade_price": str(price),
                    "best_bid": str(best_bid) if best_bid is not None else None,
                    "best_ask": str(best_ask) if best_ask is not None else None,
                    "quote_status": quote_status,
                    "currency": quote_currency,
                    "change_24h_pct": str(change * Decimal(100)),
                    "volume_24h_usd": str(volume_usd),
                    "provider_timestamp": provider_time.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "source": "Crypto.com Exchange public/get-tickers",
                    "payload_sha256": hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest(),
                })
            markets.sort(key=lambda item: Decimal(item["volume_24h_usd"]), reverse=True)
            fetched_at = self._utc_clock()
            if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
                raise CryptoComMarketOverviewError("L’horloge locale ne permet pas d’horodater la réponse.")
            snapshot = {
                "provider": "Crypto.com Exchange",
                "source": "https://api.crypto.com/exchange/v1/public/get-tickers",
                "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "count": min(len(markets), _MAX_MARKETS),
                "available_spot_count": len(markets),
                "markets": markets[:_MAX_MARKETS],
                "persistent": False,
                "refresh_after_seconds": _CACHE_SECONDS,
                "usable_for_risk": False,
                "usable_for_execution": False,
                "orders_enabled": False,
            }
            self._cached = snapshot
            self._cache_until = self._clock() + _CACHE_SECONDS
            return snapshot

    @staticmethod
    def _decimal(value: object, *, positive: bool) -> Decimal:
        if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
            raise InvalidOperation
        result = value if isinstance(value, Decimal) else Decimal(str(value))
        if not result.is_finite() or (positive and result <= 0):
            raise InvalidOperation
        return result

    @staticmethod
    def _optional_decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        return CryptoComMarketOverview._decimal(value, positive=True)


__all__ = ["CryptoComMarketOverview", "CryptoComMarketOverviewError"]
