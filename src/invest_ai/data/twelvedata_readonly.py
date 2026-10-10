"""Ephemeral, read-only Twelve Data price stream for a local market dashboard.

The API key is used only to open one provider WebSocket after an explicit local
user action. This module does not persist credentials or market observations.
"""
from __future__ import annotations

import hashlib
import json
import re
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Protocol
from urllib.parse import quote


_WEBSOCKET_BASE = "wss://ws.twelvedata.com/v1/quotes/price?apikey="
_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,31}\Z")
_MAX_SYMBOLS = 8
_MAX_EVENT_BYTES = 64 * 1024


class TwelveDataError(ValueError):
    """A safe local Twelve Data stream error."""


@dataclass(frozen=True, slots=True)
class TwelveDataPriceTick:
    symbol: str
    price: Decimal
    provider_timestamp: datetime
    received_at: datetime
    currency: str | None
    exchange: str | None
    instrument_type: str | None
    source: str
    payload_sha256: str


class _WebSocketApp(Protocol):
    url: str

    def send(self, payload: str) -> None: ...
    def run_forever(self, **kwargs: object) -> object: ...
    def close(self) -> None: ...


WebSocketFactory = Callable[..., _WebSocketApp]


class TwelveDataReadOnlyQuoteStream:
    """Manage one bounded provider stream; never executes or stores a trade."""

    def __init__(self, *, websocket_factory: WebSocketFactory | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._factory = websocket_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._app: _WebSocketApp | None = None
        self._symbols: tuple[str, ...] = ()
        self._quotes: dict[str, TwelveDataPriceTick] = {}
        self._status = "DISCONNECTED"
        self._heartbeat_stop: threading.Event | None = None

    @staticmethod
    def validate_symbols(symbols: object) -> tuple[str, ...]:
        if not isinstance(symbols, str):
            raise TwelveDataError("Enter one or more supported market symbols.")
        items = tuple(part.strip() for part in symbols.split(",") if part.strip())
        if not 1 <= len(items) <= _MAX_SYMBOLS:
            raise TwelveDataError(f"Choose between 1 and {_MAX_SYMBOLS} symbols for this local stream.")
        seen: set[str] = set()
        for symbol in items:
            if not _SYMBOL.fullmatch(symbol) or symbol.casefold() in seen:
                raise TwelveDataError("Symbols must be valid, unique provider tickers.")
            seen.add(symbol.casefold())
        return items

    def connect(self, api_key: str, symbols: str) -> dict[str, object]:
        if not isinstance(api_key, str) or not 8 <= len(api_key) <= 256 or any(ord(c) < 33 for c in api_key):
            raise TwelveDataError("Enter a valid Twelve Data API key.")
        selected = self.validate_symbols(symbols)
        with self._lock:
            if self._app is not None:
                raise TwelveDataError("A Twelve Data stream is already active; disconnect it first.")
            factory = self._factory
            if factory is None:
                try:
                    from websocket import WebSocketApp  # type: ignore[import-not-found]
                except ImportError:
                    raise TwelveDataError(
                        "The optional market-data WebSocket package is missing; install the local market-data requirements."
                    ) from None
                factory = WebSocketApp
            self._symbols = selected
            self._quotes = {}
            self._status = "CONNECTING"
            url = _WEBSOCKET_BASE + quote(api_key, safe="")
            try:
                app = factory(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
            except Exception:
                self._symbols = ()
                self._status = "ERROR"
                raise TwelveDataError("Unable to prepare the verified market-data connection.") from None
            self._app = app
            self._heartbeat_stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(app,), daemon=True,
                                            name="invest-ai-twelvedata-quotes")
            self._thread.start()
        return self.state()

    def _run(self, app: _WebSocketApp) -> None:
        with self._lock:
            stop = self._heartbeat_stop
        heartbeat = threading.Thread(target=self._heartbeat, args=(app, stop), daemon=True,
                                     name="invest-ai-twelvedata-heartbeat")
        heartbeat.start()
        try:
            app.run_forever(sslopt={"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True})
        except Exception:
            with self._lock:
                if self._app is app:
                    self._status = "ERROR"
        finally:
            if stop is not None:
                stop.set()

    def _heartbeat(self, app: _WebSocketApp, stop: threading.Event | None) -> None:
        if stop is None:
            return
        while not stop.wait(10):
            with self._lock:
                if self._app is not app or self._status != "CONNECTED":
                    return
            try:
                app.send('{"action":"heartbeat"}')
            except Exception:
                with self._lock:
                    if self._app is app:
                        self._status = "ERROR"
                return

    def _on_open(self, app: _WebSocketApp) -> None:
        with self._lock:
            if self._app is not app:
                return
            symbols = self._symbols
            self._status = "CONNECTED"
        try:
            app.send(json.dumps({"action": "subscribe", "params": {"symbols": ",".join(symbols)}},
                                separators=(",", ":")))
        except Exception:
            with self._lock:
                if self._app is app:
                    self._status = "ERROR"

    def _on_message(self, app: _WebSocketApp, raw: str) -> None:
        if not isinstance(raw, str) or len(raw.encode("utf-8", errors="replace")) > _MAX_EVENT_BYTES:
            return
        try:
            payload = json.loads(raw, parse_float=Decimal, parse_int=int,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        event = payload.get("event")
        if event != "price":
            if event == "subscribe-status" and payload.get("status") not in {"ok", "success", None}:
                with self._lock:
                    if self._app is app:
                        self._status = "PARTIAL"
            return
        symbol = payload.get("symbol")
        if not isinstance(symbol, str):
            return
        key = symbol.casefold()
        with self._lock:
            selected = {item.casefold(): item for item in self._symbols}
            if self._app is not app or key not in selected:
                return
        try:
            raw_price = payload.get("price")
            price = raw_price if isinstance(raw_price, Decimal) else Decimal(str(raw_price))
            if not price.is_finite() or price <= 0:
                return
            raw_timestamp = payload.get("timestamp")
            if type(raw_timestamp) is not int or raw_timestamp <= 0:
                return
            provider_time = datetime.fromtimestamp(raw_timestamp, timezone.utc)
            received = self._clock()
            if received.tzinfo is None or received.utcoffset() is None:
                return
            received = received.astimezone(timezone.utc)
        except (InvalidOperation, TypeError, ValueError, OverflowError, OSError):
            return
        currency = payload.get("currency")
        exchange = payload.get("exchange")
        instrument_type = payload.get("type")
        tick = TwelveDataPriceTick(
            symbol=selected[key], price=price, provider_timestamp=provider_time, received_at=received,
            currency=currency if isinstance(currency, str) and len(currency) <= 16 else None,
            exchange=exchange if isinstance(exchange, str) and len(exchange) <= 64 else None,
            instrument_type=instrument_type if isinstance(instrument_type, str) and len(instrument_type) <= 64 else None,
            source="Twelve Data WebSocket /quotes/price",
            payload_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )
        with self._lock:
            if self._app is app:
                self._quotes[key] = tick

    def _on_error(self, app: _WebSocketApp, _error: object) -> None:
        with self._lock:
            if self._app is app:
                self._status = "ERROR"

    def _on_close(self, app: _WebSocketApp, *_args: object) -> None:
        with self._lock:
            if self._app is app and self._status not in {"STOPPING", "ERROR"}:
                self._status = "DISCONNECTED"
                self._app = None
                self._thread = None
                self._heartbeat_stop = None
                self._symbols = ()
                self._quotes.clear()

    def disconnect(self) -> dict[str, object]:
        with self._lock:
            app, thread = self._app, self._thread
            self._status = "STOPPING" if app is not None else "DISCONNECTED"
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            self._app = None
            self._thread = None
            if self._heartbeat_stop is not None:
                self._heartbeat_stop.set()
            self._heartbeat_stop = None
            self._symbols = ()
            self._quotes.clear()
            self._status = "DISCONNECTED"
        return self.state()

    def state(self) -> dict[str, object]:
        with self._lock:
            quotes = sorted(self._quotes.values(), key=lambda item: item.symbol.casefold())
            return {
                "provider": "Twelve Data",
                "connection_status": self._status,
                "symbols": list(self._symbols),
                "quotes": [{
                    "symbol": tick.symbol,
                    "price": str(tick.price),
                    "provider_timestamp": tick.provider_timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "received_at": tick.received_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "currency": tick.currency,
                    "exchange": tick.exchange,
                    "instrument_type": tick.instrument_type,
                    "source": tick.source,
                    "payload_sha256": tick.payload_sha256,
                } for tick in quotes],
                "persistent": False,
                "usable_for_risk": False,
                "usable_for_execution": False,
                "orders_enabled": False,
            }


__all__ = ["TwelveDataError", "TwelveDataPriceTick", "TwelveDataReadOnlyQuoteStream"]
