"""Credential-free public market tickers from Crypto.com Exchange."""
from __future__ import annotations

import hashlib
import json
import re
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Protocol

from invest_ai.data.archive import NormalizedArchive, RawArchive
from invest_ai.data.market import MarketSnapshot, build_market_snapshot
from invest_ai.data.models import DataProvenance, Instrument, Quote
from invest_ai.data.providers import RawPayload
from invest_ai.data.snapshot_store import MarketSnapshotStore, SnapshotAssessment, SnapshotRecord


_WEBSOCKET_URL = "wss://stream.crypto.com/exchange/v1/market"
_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")
_SPOT_PAIR = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,15}_[A-Z]{3}\Z")
_MAX_SYMBOLS = 2000
# Keep a headroom below Crypto.com's per-session subscription ceiling. BUBUS2
# subscribes to both tickers and 5m candles for each tracked pair (two channels).
_MAX_CHANNELS_PER_CONNECTION = 384
_MAX_PROVIDER_FUTURE_SKEW_SECONDS = 2
_MAX_EVENT_BYTES = 64 * 1024
_SUBSCRIPTION_BATCH_SIZE = 128
_SUBSCRIPTION_PAUSE_SECONDS = 0.15


class CryptoComMarketDataError(ValueError):
    """A safe local Crypto.com public market-data error."""


class CryptoComMarketEvidenceStore:
    """Archive verified public Spot quotes and assess point-in-time Risk prices."""

    def __init__(self, project_root: str, *, clock: Callable[[], datetime] | None = None,
                 sleeper: Callable[[float], None] | None = None) -> None:
        self._raw = RawArchive(project_root)
        self._normalized = NormalizedArchive(project_root)
        self._snapshots = MarketSnapshotStore(project_root)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleeper = sleeper or time.sleep

    @property
    def snapshot_store(self) -> MarketSnapshotStore:
        return self._snapshots

    def record(self, tick: "CryptoComMarketTick", *, max_age_seconds: int = 5) -> SnapshotRecord:
        if not isinstance(tick, CryptoComMarketTick):
            raise CryptoComMarketDataError("Crypto.com ticker evidence is invalid.")
        if (not isinstance(tick.symbol, str) or not _SPOT_PAIR.fullmatch(tick.symbol)
                or not isinstance(tick.instrument_type, str)
                or tick.instrument_type.casefold() not in {"spot", "exchange ticker"}
                or tick.exchange != "Crypto.com Exchange"):
            raise CryptoComMarketDataError("Only a Crypto.com Exchange Spot pair can become market evidence.")
        if tick.quote_status != "COMPLETE" or tick.best_bid is None or tick.best_ask is None:
            raise CryptoComMarketDataError("Only a complete, non-crossed bid/ask can become a Risk price candidate.")
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 30:
            raise CryptoComMarketDataError("Market evidence age limit is invalid.")
        try:
            base, quote_currency = tick.symbol.split("_", 1)
            if not all(isinstance(value, datetime) and value.tzinfo is not None for value in
                       (tick.provider_timestamp, tick.received_at)):
                raise ValueError
            if (not isinstance(tick.price, Decimal) or not tick.price.is_finite() or tick.price <= 0
                    or not isinstance(tick.best_bid, Decimal) or not tick.best_bid.is_finite() or tick.best_bid <= 0
                    or not isinstance(tick.best_ask, Decimal) or not tick.best_ask.is_finite() or tick.best_ask <= 0
                    or tick.best_bid > tick.best_ask
                    or not isinstance(tick.payload_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", tick.payload_sha256)):
                raise ValueError
            as_of = self._clock()
            if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
                raise CryptoComMarketDataError("Local market evidence clock must be timezone-aware.")
            as_of = as_of.astimezone(timezone.utc)
            provider_time = tick.provider_timestamp.astimezone(timezone.utc)
            if provider_time > as_of:
                skew = (provider_time - as_of).total_seconds()
                if skew > _MAX_PROVIDER_FUTURE_SKEW_SECONDS:
                    raise CryptoComMarketDataError("Crypto.com quote timestamp is too far ahead of the local clock.")
                self._sleeper(skew)
                as_of = self._clock()
                if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
                    raise CryptoComMarketDataError("Local market evidence clock must be timezone-aware.")
                as_of = as_of.astimezone(timezone.utc)
                if provider_time > as_of:
                    raise CryptoComMarketDataError("Crypto.com quote timestamp remains ahead of the local clock.")
            raw_bytes = json.dumps({
                "symbol": tick.symbol, "last_trade_price": str(tick.price),
                "best_bid": str(tick.best_bid), "best_ask": str(tick.best_ask),
                "best_bid_size": str(tick.best_bid_size) if tick.best_bid_size is not None else None,
                "best_ask_size": str(tick.best_ask_size) if tick.best_ask_size is not None else None,
                "provider_timestamp": tick.provider_timestamp.isoformat(),
                "received_at": tick.received_at.isoformat(), "source_payload_sha256": tick.payload_sha256,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")
            raw = RawPayload(
                provider="crypto_com_exchange", dataset="quote", source="wss://stream.crypto.com/exchange/v1/market",
                record_id=f"{tick.symbol}:{tick.provider_timestamp.isoformat()}:{tick.payload_sha256}",
                body=raw_bytes, observed_at=tick.provider_timestamp, received_at=tick.received_at,
                available_at=tick.received_at, revision=1, instrument_id=f"crypto.com:{tick.symbol}",
                currency=quote_currency, unit=f"{quote_currency}/{base}", media_type="application/json",
            )
            archived = self._raw.archive(raw)
            provenance = DataProvenance(
                provider=raw.provider, source=raw.source, dataset=raw.dataset,
                instrument_id=raw.instrument_id, observed_at=raw.observed_at, published_at=None,
                received_at=raw.received_at, available_at=raw.available_at, revision=raw.revision,
                currency=raw.currency, unit=raw.unit, raw_reference=archived.reference,
                raw_sha256=archived.sha256, record_id=raw.record_id,
            )
            quote = Quote(provenance, tick.best_bid, tick.best_ask,
                bid_size=tick.best_bid_size, ask_size=tick.best_ask_size, venue="CRYPTOCOM")
            self._normalized.append(quote)
            instrument = Instrument(
                instrument_id=f"crypto.com:{tick.symbol}", symbol=tick.symbol, asset_type="CRYPTO",
                currency=quote_currency, exchange="Crypto.com Exchange", venue="CRYPTOCOM",
                instrument_type="SPOT", timezone="UTC", multiplier="1", price_convention="PER_UNIT",
            )
            snapshot = build_market_snapshot(quote, instrument, as_of=as_of,
                max_age=timedelta(seconds=max_age_seconds))
            return self._snapshots.register(snapshot, quote, instrument,
                max_age=timedelta(seconds=max_age_seconds))
        except CryptoComMarketDataError:
            raise
        except Exception:
            raise CryptoComMarketDataError("Crypto.com quote evidence could not be archived and verified.") from None

    def get(self, snapshot_id: str) -> SnapshotRecord:
        try:
            return self._snapshots.get(snapshot_id)
        except Exception:
            raise CryptoComMarketDataError("Verified Crypto.com market evidence is unavailable.") from None

    def assess(self, snapshot_id: str, *, as_of: datetime, max_age_seconds: int) -> SnapshotAssessment:
        try:
            return self._snapshots.assess_for_risk(snapshot_id, as_of=as_of,
                max_age=timedelta(seconds=max_age_seconds))
        except Exception:
            raise CryptoComMarketDataError("Crypto.com market evidence failed its Risk freshness assessment.") from None


@dataclass(frozen=True, slots=True)
class CryptoComMarketTick:
    symbol: str
    price: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    quote_status: str
    provider_timestamp: datetime
    received_at: datetime
    currency: str | None
    exchange: str
    instrument_type: str
    source: str
    payload_sha256: str
    best_bid_size: Decimal | None = None
    best_ask_size: Decimal | None = None


class _WebSocketApp(Protocol):
    url: str

    def send(self, payload: str) -> None: ...
    def run_forever(self, **kwargs: object) -> object: ...
    def close(self) -> None: ...


WebSocketFactory = Callable[..., _WebSocketApp]


class CryptoComPublicMarketStream:
    """Ephemeral public Exchange ticker streams; they cannot access accounts."""

    def __init__(self, *, websocket_factory: WebSocketFactory | None = None,
                 clock: Callable[[], datetime] | None = None,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self._factory = websocket_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper
        self._lock = threading.RLock()
        self._apps: dict[int, _WebSocketApp] = {}
        self._threads: dict[int, threading.Thread] = {}
        self._app_symbols: dict[int, tuple[str, ...]] = {}
        self._symbols: tuple[str, ...] = ()
        self._quotes: dict[str, CryptoComMarketTick] = {}
        self._status = "DISCONNECTED"
        self._subscription_batches: dict[int, tuple[int, tuple[str, ...]]] = {}
        self._subscribed_symbols: set[str] = set()
        self._subscription_deadline = 0.0
        self._subscription_error: str | None = None
        self._last_request_id = 0
        self._candlestick_timeframe: str | None = None
        self._candlestick_listener: Callable[[str, str, dict[str, object], float, str], None] | None = None

    def configure_candlesticks(self, timeframe: str | None, listener=None) -> None:
        """Optionally subscribe to one public candle channel alongside each ticker."""
        if timeframe not in {None, "5m", "15m", "1h", "4h", "1D"}:
            raise CryptoComMarketDataError("Unsupported Crypto.com candle timeframe.")
        if listener is not None and not callable(listener):
            raise CryptoComMarketDataError("The public candle listener must be callable.")
        with self._lock:
            if self._apps:
                raise CryptoComMarketDataError("Disconnect before changing public candle subscriptions.")
            self._candlestick_timeframe = timeframe
            self._candlestick_listener = listener

    @staticmethod
    def validate_symbols(symbols: object) -> tuple[str, ...]:
        if not isinstance(symbols, str):
            raise CryptoComMarketDataError("Enter Crypto.com Exchange instrument names, such as BTC_USDT.")
        items = tuple(part.strip().upper() for part in symbols.split(",") if part.strip())
        if not 1 <= len(items) <= _MAX_SYMBOLS:
            raise CryptoComMarketDataError(f"Choisis entre 1 et {_MAX_SYMBOLS} instruments Crypto.com.")
        seen: set[str] = set()
        for symbol in items:
            if not _SYMBOL.fullmatch(symbol) or symbol.casefold() in seen:
                raise CryptoComMarketDataError("Instrument names must be valid and unique, for example BTC_USDT.")
            seen.add(symbol.casefold())
        return items

    def connect(self, symbols: str) -> dict[str, object]:
        selected = self.validate_symbols(symbols)
        threads: list[threading.Thread] = []
        with self._lock:
            if self._apps:
                raise CryptoComMarketDataError("A Crypto.com market stream is already active; disconnect it first.")
            factory = self._factory
            if factory is None:
                try:
                    from websocket import WebSocketApp  # type: ignore[import-not-found]
                except ImportError:
                    raise CryptoComMarketDataError(
                        "The optional market-data WebSocket package is missing; install the local market-data requirements."
                    ) from None
                factory = WebSocketApp
            self._symbols = selected
            self._quotes = {}
            self._subscription_batches = {}
            self._subscribed_symbols = set()
            self._subscription_error = None
            self._subscription_deadline = time.monotonic() + 45
            self._status = "CONNECTING"
            try:
                channels_per_symbol = 2 if self._candlestick_timeframe is not None else 1
                max_symbols_per_connection = _MAX_CHANNELS_PER_CONNECTION // channels_per_symbol
                for group_index, offset in enumerate(range(0, len(selected), max_symbols_per_connection)):
                    group = selected[offset:offset + max_symbols_per_connection]
                    app = factory(_WEBSOCKET_URL, on_open=self._on_open, on_message=self._on_message,
                                  on_error=self._on_error, on_close=self._on_close)
                    key = id(app)
                    self._apps[key] = app
                    self._app_symbols[key] = group
                    thread = threading.Thread(target=self._run, args=(app,), daemon=True,
                        name=f"bubus-cryptocom-market-{group_index + 1}")
                    self._threads[key] = thread
                    threads.append(thread)
            except Exception:
                apps = tuple(self._apps.values())
                self._apps.clear(); self._threads.clear(); self._app_symbols.clear()
                self._symbols = ()
                self._subscription_deadline = 0.0
                self._status = "ERROR"
                for app in apps:
                    try: app.close()
                    except Exception: pass
                raise CryptoComMarketDataError("Unable to prepare the public Crypto.com market connection.") from None
            for thread in threads:
                thread.start()
        return self.state()

    def _run(self, app: _WebSocketApp) -> None:
        try:
            app.run_forever(sslopt={"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True})
        except Exception:
            with self._lock:
                if id(app) in self._apps:
                    self._status = "ERROR"

    def _on_open(self, app: _WebSocketApp) -> None:
        key = id(app)
        with self._lock:
            if key not in self._apps or self._status == "STOPPING":
                return
            symbols = self._app_symbols[key]
        try:
            # Crypto.com recommends a brief pause after opening before requests.
            self._sleep(1)
            with self._lock:
                if key not in self._apps or self._status == "STOPPING":
                    return
                self._status = "SUBSCRIBING"
            symbols_per_batch = (_SUBSCRIPTION_BATCH_SIZE // 2
                                 if self._candlestick_timeframe is not None else _SUBSCRIPTION_BATCH_SIZE)
            for offset in range(0, len(symbols), symbols_per_batch):
                batch = symbols[offset:offset + symbols_per_batch]
                with self._lock:
                    if key not in self._apps or self._status in {"STOPPING", "ERROR"}:
                        return
                    self._last_request_id = max(int(time.time() * 1000), self._last_request_id + 1)
                    current_id = self._last_request_id
                    self._subscription_batches[current_id] = (key, batch)
                channels = [f"ticker.{symbol}" for symbol in batch]
                if self._candlestick_timeframe is not None:
                    channels.extend(f"candlestick.{self._candlestick_timeframe}.{symbol}" for symbol in batch)
                app.send(json.dumps({
                    "id": current_id,
                    "method": "subscribe",
                    "nonce": int(time.time() * 1000),
                    "params": {"channels": channels},
                }, separators=(",", ":")))
                if offset + symbols_per_batch < len(symbols):
                    self._sleep(_SUBSCRIPTION_PAUSE_SECONDS)
            with self._lock:
                if key in self._apps and self._subscription_batches:
                    self._subscription_deadline = max(self._subscription_deadline, time.monotonic() + 30)
        except Exception:
            with self._lock:
                if key in self._apps:
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
        response_id = payload.get("id")
        if type(response_id) is int:
            with self._lock:
                pending = self._subscription_batches.get(response_id)
                batch = pending[1] if pending is not None and pending[0] == id(app) else None
                if batch is not None:
                    self._subscription_batches.pop(response_id, None)
                    if type(payload.get("code")) is not int or payload.get("code") != 0:
                        self._status = "ERROR"
                        self._subscription_error = "Crypto.com a refusé un lot d’abonnements publics."
                        self._quotes.clear()
                        for active_app in self._apps.values():
                            try: active_app.close()
                            except Exception: pass
                        return
                    self._subscribed_symbols.update(batch)
                    if (not self._subscription_batches
                            and len(self._subscribed_symbols) == len(self._symbols)):
                        self._status = "CONNECTED"
                        self._subscription_deadline = 0.0
        if payload.get("method") == "public/heartbeat" and type(payload.get("id")) is int:
            with self._lock:
                if id(app) not in self._apps:
                    return
            try:
                app.send(json.dumps({"id": payload["id"], "method": "public/respond-heartbeat"},
                                    separators=(",", ":")))
            except Exception:
                self._on_error(app, "heartbeat response failed")
            return
        result = payload.get("result")
        if not isinstance(result, dict):
            return
        if result.get("channel") == "candlestick":
            self._on_candlestick(app, result, raw)
            return
        if result.get("channel") != "ticker":
            return
        with self._lock:
            if id(app) not in self._apps or self._status in {"ERROR", "STOPPING"}:
                return
            selected = {symbol.casefold(): symbol for symbol in self._symbols}
        rows = result.get("data")
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("i") or result.get("instrument_name")
            if not isinstance(symbol, str) or symbol.casefold() not in selected:
                continue
            try:
                raw_price = row.get("a")
                price = raw_price if isinstance(raw_price, Decimal) else Decimal(str(raw_price))
                raw_bid, raw_ask = row.get("b"), row.get("k")
                best_bid = self._optional_price(raw_bid)
                best_ask = self._optional_price(raw_ask)
                best_bid_size = self._optional_size(row.get("bs"))
                best_ask_size = self._optional_size(row.get("ks"))
                timestamp = row.get("t")
                if isinstance(timestamp, str) and timestamp.isdigit():
                    timestamp = int(timestamp)
                if type(timestamp) is not int or timestamp <= 0 or not price.is_finite() or price <= 0:
                    continue
                provider_time = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
                received = self._clock()
                if received.tzinfo is None or received.utcoffset() is None:
                    continue
                received = received.astimezone(timezone.utc)
            except (InvalidOperation, TypeError, ValueError, OverflowError, OSError):
                continue
            parts = symbol.split("_")
            quote_status = "INCOMPLETE"
            if best_bid is not None and best_ask is not None:
                quote_status = "CROSSED" if best_bid > best_ask else "COMPLETE"
            tick = CryptoComMarketTick(
                symbol=selected[symbol.casefold()], price=price, best_bid=best_bid, best_ask=best_ask,
                best_bid_size=best_bid_size, best_ask_size=best_ask_size,
                quote_status=quote_status,
                provider_timestamp=provider_time, received_at=received,
                currency=parts[-1] if len(parts) > 1 else None,
                exchange="Crypto.com Exchange",
                instrument_type="Perpetual" if "-PERP" in symbol else "Exchange ticker",
                source="Crypto.com Exchange public ticker WebSocket",
                payload_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            )
            with self._lock:
                if id(app) in self._apps:
                    self._quotes[tick.symbol.casefold()] = tick

    def _on_candlestick(self, app: _WebSocketApp, result: dict[str, object], raw: str) -> None:
        with self._lock:
            if id(app) not in self._apps or self._status in {"ERROR", "STOPPING"}:
                return
            timeframe = self._candlestick_timeframe
            listener = self._candlestick_listener
            selected = {symbol.casefold(): symbol for symbol in self._symbols}
        if timeframe is None or listener is None:
            return
        symbol = result.get("instrument_name")
        if not isinstance(symbol, str) or symbol.casefold() not in selected:
            return
        intervals = {"M5": "5m", "M15": "15m", "H1": "1h", "H4": "4h", "D1": "1D"}
        interval = result.get("interval")
        subscription = result.get("subscription")
        if intervals.get(interval) != timeframe:
            if not isinstance(subscription, str) or subscription != f"candlestick.{timeframe}.{selected[symbol.casefold()]}" or interval is not None:
                return
        rows = result.get("data")
        if not isinstance(rows, list):
            return
        received_at = self._clock()
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            return
        received_epoch = received_at.timestamp()
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                candle = {name: row[name] for name in ("o", "h", "l", "c", "v", "t")}
                if type(candle["t"]) is not int or candle["t"] <= 0:
                    continue
                listener(selected[symbol.casefold()], timeframe, candle, received_epoch, digest)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue

    def _on_error(self, app: _WebSocketApp, _error: object) -> None:
        with self._lock:
            if id(app) in self._apps:
                self._status = "ERROR"
                self._subscription_error = "La connexion publique Crypto.com a échoué."
                self._quotes.clear()
                for active_app in self._apps.values():
                    if active_app is not app:
                        try: active_app.close()
                        except Exception: pass

    def _on_close(self, app: _WebSocketApp, *_args: object) -> None:
        with self._lock:
            if id(app) in self._apps and self._status not in {"STOPPING", "ERROR"}:
                self._status = "ERROR"
                self._subscription_error = "La connexion publique Crypto.com a été interrompue."
                self._quotes.clear()

    def disconnect(self) -> dict[str, object]:
        with self._lock:
            apps = tuple(self._apps.values())
            threads = tuple(self._threads.values())
            self._status = "STOPPING" if apps else "DISCONNECTED"
        for app in apps:
            try:
                app.close()
            except Exception:
                pass
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        with self._lock:
            self._apps.clear()
            self._threads.clear()
            self._app_symbols.clear()
            self._symbols = ()
            self._subscription_batches.clear()
            self._subscribed_symbols.clear()
            self._subscription_deadline = 0.0
            self._subscription_error = None
            self._quotes.clear()
            self._status = "DISCONNECTED"
        return self.state()

    def state(self) -> dict[str, object]:
        with self._lock:
            if (self._status in {"CONNECTING", "SUBSCRIBING"} and self._subscription_deadline
                    and time.monotonic() > self._subscription_deadline):
                self._status = "ERROR"
                self._subscription_error = "L’accusé d’abonnement Crypto.com n’est pas arrivé à temps."
            quotes = sorted(self._quotes.values(), key=lambda item: item.symbol.casefold())
            return {
                "provider": "Crypto.com Exchange",
                "connection_status": self._status,
                "symbols": list(self._symbols),
                "subscribed_symbols": sorted(self._subscribed_symbols, key=str.casefold),
                "subscription_pending_count": len(self._subscription_batches),
                "subscription_error": self._subscription_error,
                "candlestick_timeframe": self._candlestick_timeframe,
                "quotes": [{
                    "symbol": tick.symbol,
                    "price": str(tick.price),
                    "last_trade_price": str(tick.price),
                    "best_bid": str(tick.best_bid) if tick.best_bid is not None else None,
                    "best_ask": str(tick.best_ask) if tick.best_ask is not None else None,
                    "best_bid_size": str(tick.best_bid_size) if tick.best_bid_size is not None else None,
                    "best_ask_size": str(tick.best_ask_size) if tick.best_ask_size is not None else None,
                    "quote_status": tick.quote_status,
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

    def get_tick(self, symbol: str) -> CryptoComMarketTick | None:
        """Return one typed live quote only while its public stream is connected."""
        if not isinstance(symbol, str):
            return None
        with self._lock:
            if self._status != "CONNECTED":
                return None
            return self._quotes.get(symbol.casefold())

    @staticmethod
    def _optional_price(value: object) -> Decimal | None:
        if value is None:
            return None
        if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
            raise InvalidOperation
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        if not parsed.is_finite() or parsed <= 0:
            raise InvalidOperation
        return parsed

    @staticmethod
    def _optional_size(value: object) -> Decimal | None:
        if value is None:
            return None
        if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
            raise InvalidOperation
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        if not parsed.is_finite() or parsed < 0:
            raise InvalidOperation
        return parsed


__all__ = ["CryptoComMarketDataError", "CryptoComMarketEvidenceStore", "CryptoComMarketTick",
           "CryptoComPublicMarketStream"]
