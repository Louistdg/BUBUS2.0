"""Canonical, provider-neutral financial data contracts.

All timestamps are timezone-aware and stored as UTC. Monetary and market
numeric values use Decimal; floats are rejected to avoid binary rounding.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath, PureWindowsPath
from typing import TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class DataContractError(ValueError):
    """An input payload does not meet the canonical data contract."""


def as_utc(value: datetime | str, field_name: str) -> datetime:
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise DataContractError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DataContractError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


def as_decimal(value: Decimal | int | str | None, field_name: str,
               *, optional: bool = False) -> Decimal | None:
    if value is None:
        if optional:
            return None
        raise DataContractError(f"{field_name} is required")
    if isinstance(value, bool) or isinstance(value, float):
        raise DataContractError(f"{field_name} must not be a float or bool; use Decimal/string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DataContractError(f"{field_name} must be a decimal value") from exc
    if not result.is_finite():
        raise DataContractError(f"{field_name} must be finite")
    return result


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataContractError(f"{name} is required")
    return value.strip()


@dataclass(frozen=True, slots=True)
class DataProvenance:
    provider: str
    source: str
    dataset: str
    instrument_id: str | None
    observed_at: datetime
    published_at: datetime | None
    received_at: datetime
    available_at: datetime
    revision: int
    currency: str | None
    unit: str | None
    raw_reference: str
    raw_sha256: str
    record_id: str

    def __post_init__(self) -> None:
        for name in ("provider", "source", "dataset", "raw_reference", "record_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(self, "observed_at", as_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "received_at", as_utc(self.received_at, "received_at"))
        object.__setattr__(self, "available_at", as_utc(self.available_at, "available_at"))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", as_utc(self.published_at, "published_at"))
        if self.available_at < self.received_at:
            raise DataContractError("available_at cannot precede received_at")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise DataContractError("revision must be a positive integer")
        if self.currency is not None:
            currency = self.currency.strip().upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", currency):
                raise DataContractError("currency must be a valid uppercase currency or asset code")
            object.__setattr__(self, "currency", currency)
        if self.unit is not None:
            object.__setattr__(self, "unit", _required_text(self.unit, "unit"))
        normalized_ref = self.raw_reference.replace("\\", "/")
        ref = PurePosixPath(normalized_ref)
        win_ref = PureWindowsPath(self.raw_reference)
        if ref.is_absolute() or win_ref.is_absolute() or win_ref.drive or ".." in ref.parts:
            raise DataContractError("raw_reference must be a safe relative path")
        if not re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256):
            raise DataContractError("raw_sha256 must be a lowercase SHA-256 hex digest")

    @property
    def logical_key(self) -> str:
        parts = (self.provider, self.dataset, self.instrument_id or "", self.record_id)
        return "|".join(parts)

    @property
    def observation_id(self) -> str:
        value = f"{self.logical_key}|revision={self.revision}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    symbol: str
    asset_type: str
    currency: str
    exchange: str | None = None
    mic: str | None = None
    isin: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    timezone: str = "UTC"
    instrument_type: str | None = None
    multiplier: Decimal | int | str = Decimal("1")
    tick_size: Decimal | int | str | None = None
    lot_size: Decimal | int | str | None = None
    calendar_id: str | None = None
    price_convention: str = "PER_UNIT"
    venue: str | None = None

    def __post_init__(self) -> None:
        for name in ("instrument_id", "symbol", "asset_type", "currency"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        currency = self.currency.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", currency):
            raise DataContractError("currency must be a valid uppercase currency or asset code")
        object.__setattr__(self, "currency", currency)
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            # Windows Python may not ship the IANA database. Keep canonical
            # IANA-style names usable without silently mapping a different zone;
            # deployments needing offset/DST conversion must provide tzdata.
            if self.timezone != "UTC" and not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9._+-]*(?:/[A-Za-z0-9._+-]+)+", self.timezone
            ):
                raise DataContractError("timezone must be a valid IANA timezone name") from exc
        except (TypeError, ValueError) as exc:
            raise DataContractError("timezone must be a valid IANA timezone") from exc
        object.__setattr__(self, "instrument_type", self.instrument_type or self.asset_type)
        object.__setattr__(self, "multiplier", as_decimal(self.multiplier, "multiplier"))
        if self.multiplier <= 0:
            raise DataContractError("multiplier must be positive")
        for name in ("tick_size", "lot_size"):
            value = as_decimal(getattr(self, name), name, optional=True)
            if value is not None and value <= 0:
                raise DataContractError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "price_convention", _required_text(self.price_convention, "price_convention"))
        for name in ("exchange", "venue", "calendar_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_text(value, name))
        for name in ("valid_from", "valid_to"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, as_utc(value, name))
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise DataContractError("valid_to cannot precede valid_from")


@dataclass(frozen=True, slots=True)
class MarketBar:
    provenance: DataProvenance
    interval: str
    bar_start: datetime
    bar_end: datetime
    open: Decimal | int | str | None
    high: Decimal | int | str | None
    low: Decimal | int | str | None
    close: Decimal | int | str | None
    volume: Decimal | int | str | None = None
    adjusted: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "interval", _required_text(self.interval, "interval"))
        if self.adjusted is not None and not isinstance(self.adjusted, bool):
            raise DataContractError("adjusted must be true, false, or unknown")
        object.__setattr__(self, "bar_start", as_utc(self.bar_start, "bar_start"))
        object.__setattr__(self, "bar_end", as_utc(self.bar_end, "bar_end"))
        if self.bar_end <= self.bar_start:
            raise DataContractError("bar_end must be after bar_start")
        for name in ("open", "high", "low", "close"):
            value = as_decimal(getattr(self, name), name, optional=True)
            if value is not None and value <= 0:
                raise DataContractError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        volume = as_decimal(self.volume, "volume", optional=True)
        if volume is not None and volume < 0:
            raise DataContractError("volume cannot be negative")
        object.__setattr__(self, "volume", volume)
        values = (self.open, self.high, self.low, self.close)
        if all(value is not None for value in values):
            if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
                raise DataContractError("OHLC bounds are inconsistent")
            if self.high < self.low:
                raise DataContractError("high cannot be below low")

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return tuple(name for name in ("open", "high", "low", "close")
                     if getattr(self, name) is None)

    @property
    def is_usable(self) -> bool:
        return not self.missing_fields


@dataclass(frozen=True, slots=True)
class Quote:
    provenance: DataProvenance
    bid: Decimal | int | str | None
    ask: Decimal | int | str | None
    bid_size: Decimal | int | str | None = None
    ask_size: Decimal | int | str | None = None
    venue: str | None = None

    def __post_init__(self) -> None:
        for name in ("bid", "ask"):
            value = as_decimal(getattr(self, name), name, optional=True)
            if value is not None and value <= 0:
                raise DataContractError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        if self.bid is None and self.ask is None:
            raise DataContractError("at least one of bid or ask is required")
        for name in ("bid_size", "ask_size"):
            value = as_decimal(getattr(self, name), name, optional=True)
            if value is not None and value < 0:
                raise DataContractError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)

    @property
    def is_complete(self) -> bool:
        return self.bid is not None and self.ask is not None

    @property
    def is_crossed(self) -> bool:
        if not self.is_complete:
            return False
        return self.bid > self.ask


@dataclass(frozen=True, slots=True)
class FundamentalFact:
    provenance: DataProvenance
    metric: str
    value: Decimal | int | str
    period_start: date | None = None
    period_end: date | None = None
    fiscal_period: str | None = None
    accession: str | None = None
    xbrl_tag: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric", _required_text(self.metric, "metric"))
        object.__setattr__(self, "value", as_decimal(self.value, "value"))
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise DataContractError("period_end cannot precede period_start")


@dataclass(frozen=True, slots=True)
class MacroObservation:
    provenance: DataProvenance
    series_id: str
    period: str
    frequency: str
    value: Decimal | int | str | None
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("series_id", "period", "frequency"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(self, "value", as_decimal(self.value, "value", optional=True))
        if self.value is None and not self.missing_reason:
            raise DataContractError("missing macro values require missing_reason")
        if self.missing_reason is not None:
            object.__setattr__(self, "missing_reason", self.missing_reason.strip())


@dataclass(frozen=True, slots=True)
class NewsItem:
    provenance: DataProvenance
    title: str
    publisher: str
    url: str
    body: str | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        for name in ("title", "publisher", "url"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class Filing:
    provenance: DataProvenance
    accession: str
    form: str
    filed_at: datetime
    title: str
    document_url: str
    accepted_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("accession", "form", "title", "document_url"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(self, "filed_at", as_utc(self.filed_at, "filed_at"))
        if self.accepted_at is not None:
            object.__setattr__(self, "accepted_at", as_utc(self.accepted_at, "accepted_at"))


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    provider: str
    adapter_version: str
    supported_datasets: tuple[str, ...]
    read_only: bool
    supports_realtime: bool
    supports_websocket: bool
    terms_url: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(self, "adapter_version", _required_text(self.adapter_version, "adapter_version"))
        if not self.read_only:
            raise DataContractError("V0 provider adapters must be read-only")
        if self.supports_websocket and not self.supports_realtime:
            raise DataContractError("WebSocket support requires a realtime capability")
        object.__setattr__(self, "supported_datasets", tuple(
            _required_text(item, "supported dataset") for item in self.supported_datasets
        ))


DataObservation: TypeAlias = MarketBar | Quote | FundamentalFact | MacroObservation | NewsItem | Filing


def observation_to_dict(observation: DataObservation) -> dict[str, object]:
    """Serialize a canonical observation using JSON-safe UTC and decimal strings."""
    from dataclasses import fields, is_dataclass

    def convert(value: object) -> object:
        if isinstance(value, datetime):
            return as_utc(value, "timestamp").isoformat().replace("+00:00", "Z")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, tuple):
            return [convert(item) for item in value]
        if is_dataclass(value):
            return {field.name: convert(getattr(value, field.name)) for field in fields(value)}
        if isinstance(value, (str, int, bool)) or value is None:
            return value
        raise DataContractError(f"unsupported serialization type: {type(value).__name__}")

    return convert(observation)  # type: ignore[return-value]


def canonical_json_bytes(observation: DataObservation) -> bytes:
    return json.dumps(observation_to_dict(observation), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")
