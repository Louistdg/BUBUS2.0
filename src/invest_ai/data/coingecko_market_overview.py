"""Ephemeral, attributed global crypto market overview from CoinGecko's keyless API."""
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


_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1&sparkline=false&price_change_percentage=24h"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_CACHE_SECONDS = 60
_MAX_MARKETS = 250
_COIN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
_SYMBOL = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}\Z", re.IGNORECASE)


class CoinGeckoMarketOverviewError(ValueError):
    """Safe error for a keyless public CoinGecko request."""


class _Response(Protocol):
    def read(self, amount: int = -1) -> bytes: ...
    def __enter__(self) -> "_Response": ...
    def __exit__(self, *_args: object) -> object: ...


class CoinGeckoMarketOverview:
    """Fetch a cached page of 250 actual market-cap-ranked coins; never persists."""

    def __init__(self, *, opener: Callable[..., _Response] | None = None,
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
                _MARKETS_URL,
                headers={"Accept": "application/json", "User-Agent": "INVEST-AI-read-only-market/1"},
                method="GET",
            )
            try:
                with self._opener(request, timeout=12) as response:
                    raw = response.read(_MAX_RESPONSE_BYTES + 1)
            except Exception:
                raise CoinGeckoMarketOverviewError(
                    "CoinGecko ne répond pas; aucun cours de marché n’a été conservé."
                ) from None
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise CoinGeckoMarketOverviewError("La réponse publique de CoinGecko dépasse la taille autorisée.")
            try:
                payload = json.loads(raw, parse_float=Decimal, parse_int=int,
                                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, TypeError):
                raise CoinGeckoMarketOverviewError("Réponse de marché CoinGecko invalide.") from None
            if not isinstance(payload, list):
                raise CoinGeckoMarketOverviewError("CoinGecko n’a pas fourni une liste de marchés.")

            markets: list[dict[str, object]] = []
            for row in payload[:_MAX_MARKETS]:
                if not isinstance(row, dict):
                    continue
                coin_id, symbol, name = row.get("id"), row.get("symbol"), row.get("name")
                if (not isinstance(coin_id, str) or not _COIN_ID.fullmatch(coin_id)
                        or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                        or not isinstance(name, str) or not name.strip() or len(name) > 160):
                    continue
                try:
                    price = self._decimal(row.get("current_price"), positive=True)
                    volume = self._decimal(row.get("total_volume"), positive=False)
                    market_cap = self._decimal(row.get("market_cap"), positive=False)
                    change_value = row.get("price_change_percentage_24h")
                    change = None if change_value is None else self._decimal(change_value, positive=False)
                    rank = row.get("market_cap_rank")
                    if volume < 0 or market_cap < 0 or (rank is not None and (type(rank) is not int or rank < 1)):
                        continue
                    timestamp = row.get("last_updated")
                    if not isinstance(timestamp, str):
                        continue
                    provider_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if provider_time.tzinfo is None or provider_time.utcoffset() is None:
                        continue
                except (InvalidOperation, TypeError, ValueError, OverflowError, OSError):
                    continue
                markets.append({
                    "id": coin_id,
                    "symbol": symbol.upper(),
                    "name": name.strip(),
                    "price_usd": str(price),
                    "market_cap_usd": str(market_cap),
                    "market_cap_rank": rank,
                    "volume_24h_usd": str(volume),
                    "change_24h_pct": None if change is None else str(change),
                    "provider_timestamp": provider_time.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "source": "CoinGecko API /coins/markets",
                    "payload_sha256": hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest(),
                })
            fetched_at = self._utc_clock()
            if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
                raise CoinGeckoMarketOverviewError("L’horloge locale ne permet pas d’horodater la réponse.")
            snapshot = {
                "provider": "CoinGecko",
                "source": "https://api.coingecko.com/api/v3/coins/markets",
                "attribution_text": "Données fournies par CoinGecko",
                "attribution_url": "https://www.coingecko.com/",
                "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "count": len(markets),
                "markets": markets,
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


__all__ = ["CoinGeckoMarketOverview", "CoinGeckoMarketOverviewError"]
