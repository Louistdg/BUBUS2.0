"""Canonical, deterministic market snapshots derived from normalized observations."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from invest_ai.data.models import (
    DataContractError, DataProvenance, Instrument, MarketBar, Quote, as_decimal,
    as_utc, canonical_json_bytes,
)


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    instrument_id: str
    symbol: str
    instrument_type: str
    price: Decimal
    currency: str
    venue: str
    timezone: str
    multiplier: Decimal
    tick_size: Decimal | None
    lot_size: Decimal | None
    calendar_id: str | None
    price_convention: str
    adjusted: bool | None
    provider: str
    source: str
    dataset: str
    observed_at: datetime
    published_at: datetime | None
    received_at: datetime
    available_at: datetime
    revision: int
    raw_reference: str
    raw_sha256: str
    record_id: str
    as_of: datetime
    stale: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", as_decimal(self.price, "price"))
        if self.price <= 0:
            raise DataContractError("snapshot price must be positive")
        for name in ("observed_at", "received_at", "available_at", "as_of"):
            object.__setattr__(self, name, as_utc(getattr(self, name), name))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", as_utc(self.published_at, "published_at"))
        if self.observed_at > self.as_of or self.available_at > self.as_of:
            raise DataContractError("future market data is not eligible for this snapshot")

    @property
    def snapshot_id(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


def build_market_snapshot(
    observation: MarketBar | Quote,
    instrument: Instrument,
    *,
    as_of: datetime | str,
    max_age: timedelta,
) -> MarketSnapshot:
    """Build a point-in-time snapshot from a normalized bar or two-sided quote.

    A bar contributes its close. A quote contributes its midpoint. Venue and
    currency are mandatory at this boundary; stale observations remain visible
    in the result with ``stale=True`` so callers can fail closed explicitly.
    """
    if max_age < timedelta(0):
        raise DataContractError("max_age cannot be negative")
    provenance: DataProvenance = observation.provenance
    cutoff = as_utc(as_of, "as_of")
    if provenance.instrument_id != instrument.instrument_id:
        raise DataContractError("observation instrument does not match instrument metadata")
    if provenance.currency is None or provenance.currency != instrument.currency:
        raise DataContractError("currency mismatch or missing currency")
    venue = (observation.venue if isinstance(observation, Quote) else None) or instrument.venue or instrument.exchange
    if not venue:
        raise DataContractError("venue is required to build a market snapshot")
    if (provenance.observed_at > cutoff or provenance.available_at > cutoff
            or (provenance.published_at is not None and provenance.published_at > cutoff)):
        raise DataContractError("future market data is not eligible for this snapshot")

    if isinstance(observation, MarketBar):
        if observation.close is None:
            raise DataContractError("market bar close is required for a snapshot")
        price = observation.close
        adjusted = observation.adjusted
    else:
        if not observation.is_complete:
            raise DataContractError("two-sided quote is required for a market snapshot")
        if observation.is_crossed:
            raise DataContractError("crossed quote cannot produce a market snapshot")
        price = (observation.bid + observation.ask) / Decimal(2)
        adjusted = None

    # Tick size constrains executable quoted/bar prices, while a quote midpoint
    # is a derived reference and can legitimately fall between ticks.
    if instrument.tick_size is not None and isinstance(observation, MarketBar):
        if price % instrument.tick_size != 0:
            raise DataContractError("price is not aligned to instrument tick size")

    stale = provenance.observed_at < cutoff - max_age
    return MarketSnapshot(
        instrument_id=instrument.instrument_id,
        symbol=instrument.symbol,
        instrument_type=instrument.instrument_type or instrument.asset_type,
        price=price,
        currency=instrument.currency,
        venue=venue,
        timezone=instrument.timezone,
        multiplier=instrument.multiplier,
        tick_size=instrument.tick_size,
        lot_size=instrument.lot_size,
        calendar_id=instrument.calendar_id,
        price_convention=instrument.price_convention,
        adjusted=adjusted,
        provider=provenance.provider,
        source=provenance.source,
        dataset=provenance.dataset,
        observed_at=provenance.observed_at,
        published_at=provenance.published_at,
        received_at=provenance.received_at,
        available_at=provenance.available_at,
        revision=provenance.revision,
        raw_reference=provenance.raw_reference,
        raw_sha256=provenance.raw_sha256,
        record_id=provenance.record_id,
        as_of=cutoff,
        stale=stale,
    )
