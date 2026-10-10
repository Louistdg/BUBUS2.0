"""Read-only Crypto.com Exchange instrument rules for current Spot markets."""
from __future__ import annotations

import json
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Callable, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_URL = "https://api.crypto.com/exchange/v1/public/get-instruments"
_TICKER_URL = "https://api.crypto.com/exchange/v1/public/get-tickers"
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,15}_[A-Z]{3,12}\Z")
_MAX_BYTES = 4 *  1024 * 1024
_MINIMUM_SPOT_NOTIONAL_USD = Decimal("1")
_MAX_TICKER_AGE_SECONDS = 5


class CryptoComInstrumentCatalogError(ValueError):
    """The public instrument catalog is unavailable or invalid."""


class _Response(Protocol):
    def read(self, amount: int = -1) -> bytes: ...
    def __enter__(self) -> "_Response": ...
    def __exit__(self, *_args: object) -> object: ...


class CryptoComInstrumentCatalog:
    """Cache the official public instrument metadata; never handles credentials."""

    def __init__(self, *, opener: Callable[..., _Response] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time, cache_seconds: int = 60) -> None:
        self._opener = opener or urlopen
        self._clock = clock
        self._wall_clock = wall_clock
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cache_until = 0.0
        self._cached: dict[str, dict[str, str]] | None = None

    def get_spot_catalog(self) -> dict[str, dict[str, str]]:
        with self._lock:
            now = self._clock()
            if self._cached is not None and now < self._cache_until:
                return self._cached
            request = Request(_URL, headers={"Accept": "application/json", "User-Agent": "BUBUS2.0/2"}, method="GET")
            try:
                with self._opener(request, timeout=10) as response:
                    raw = response.read(_MAX_BYTES + 1)
                if len(raw) > _MAX_BYTES:
                    raise ValueError
                payload = json.loads(raw, parse_float=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                rows = payload.get("result", {}).get("data") if isinstance(payload, dict) else None
                if payload.get("code") != 0 or not isinstance(rows, list):
                    raise ValueError
                catalog: dict[str, dict[str, str]] = {}
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    symbol = row.get("symbol")
                    inst_type = row.get("inst_type")
                    if (not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                            or not isinstance(inst_type, str) or inst_type.upper() != "CCY_PAIR"
                            or row.get("tradable") is not True):
                        continue
                    base, quote = symbol.split("_", 1)
                    if row.get("base_ccy") != base or row.get("quote_ccy") != quote:
                        continue
                    try:
                        price_tick = self._positive_decimal(row.get("price_tick_size"))
                        quantity_tick = self._positive_decimal(row.get("qty_tick_size"))
                    except (InvalidOperation, TypeError, ValueError):
                        continue
                    catalog[symbol] = {
                        "base_currency": base,
                        "quote_currency": quote,
                        "price_tick_size": str(price_tick),
                        "quantity_tick_size": str(quantity_tick),
                        "tradable": "true",
                    }
                self._cached = catalog
                self._cache_until = self._clock() + self._cache_seconds
                return catalog
            except Exception:
                raise CryptoComInstrumentCatalogError("Le catalogue officiel Crypto.com est indisponible ou invalide.") from None

    def assert_spot_limit_order(self, intent: object) -> None:
        """Implement the order adapter's instrument verifier using current Exchange rules."""
        from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder, CryptoComSpotOrderError

        if not isinstance(intent, CryptoComSpotLimitOrder):
            raise CryptoComSpotOrderError("A validated Crypto.com Spot LIMIT intent is required.")
        rules = self.get_spot_catalog().get(intent.instrument_name)
        if rules is None:
            raise CryptoComSpotOrderError("The pair is not currently tradable as a Crypto.com Spot currency pair.")
        price_tick = Decimal(rules["price_tick_size"])
        quantity_tick = Decimal(rules["quantity_tick_size"])
        if intent.price % price_tick != 0:
            raise CryptoComSpotOrderError("Limit price does not align with the current Crypto.com price tick.")
        if intent.quantity % quantity_tick != 0:
            raise CryptoComSpotOrderError("Order quantity does not align with the current Crypto.com quantity tick.")
        effective_price = intent.price
        if intent.side == "BUY":
            best_ask = self._fresh_ticker(intent.instrument_name)["ask"]
            effective_price = min(effective_price, best_ask)
        quote_to_usd = self._quote_usd_rate(rules["quote_currency"])
        if effective_price * intent.quantity * quote_to_usd < _MINIMUM_SPOT_NOTIONAL_USD:
            raise CryptoComSpotOrderError(
                "Crypto.com Spot orders require at least 1 USD of notional value."
            )

    def _quote_usd_rate(self, quote_currency: str) -> Decimal:
        if quote_currency == "USD":
            return Decimal(1)
        from invest_ai.execution.cryptocom_spot import CryptoComSpotOrderError

        symbol = f"{quote_currency}_USD"
        if symbol not in self.get_spot_catalog():
            raise CryptoComSpotOrderError(
                "The quote currency has no current tradable Spot pair to USD; minimum notional cannot be verified."
            )
        return self._fresh_ticker(symbol)["bid"]

    def _fresh_ticker(self, symbol: str) -> dict[str, Decimal]:
        from invest_ai.execution.cryptocom_spot import CryptoComSpotOrderError

        url = f"{_TICKER_URL}?{urlencode({'instrument_name': symbol})}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "BUBUS2.0/2"}, method="GET")
        try:
            with self._opener(request, timeout=5) as response:
                raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise ValueError
            payload = json.loads(raw, parse_float=Decimal,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            rows = payload.get("result", {}).get("data") if isinstance(payload, dict) else None
            if (payload.get("code") != 0 or not isinstance(rows, list) or len(rows) != 1
                    or not isinstance(rows[0], dict) or rows[0].get("i") != symbol):
                raise ValueError
            bid = self._positive_decimal(rows[0].get("b"))
            ask = self._positive_decimal(rows[0].get("k"))
            if bid > ask:
                raise ValueError
            published_ms = self._timestamp_ms(rows[0].get("t"))
            now = self._wall_clock()
            age = now - published_ms / 1000
            if (not isinstance(now, (int, float)) or isinstance(now, bool)
                    or age < -1 or age > _MAX_TICKER_AGE_SECONDS):
                raise ValueError
            return {"bid": bid, "ask": ask}
        except Exception:
            raise CryptoComSpotOrderError(
                "A fresh Crypto.com ticker is required to check the Spot minimum."
            ) from None

    @staticmethod
    def _timestamp_ms(value: object) -> int:
        if type(value) is int and 0 <= value <= 9_999_999_999_999:
            return value
        if isinstance(value, str) and value.isdigit() and len(value) <= 13:
            return int(value)
        raise ValueError

    @staticmethod
    def _positive_decimal(value: object) -> Decimal:
        if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
            raise InvalidOperation
        result = value if isinstance(value, Decimal) else Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise InvalidOperation
        return result


__all__ = ["CryptoComInstrumentCatalog", "CryptoComInstrumentCatalogError"]
