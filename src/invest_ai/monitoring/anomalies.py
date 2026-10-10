"""Deterministic, observation-only anomaly detection for replayable market data.

This module emits review alerts from canonical bars and quotes. It does not
create trade proposals, authorize orders, persist market data, or connect to a
provider. Quote sizes are best-bid/best-ask sizes only, never full depth.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TypeAlias

from invest_ai.data.market import MarketSnapshot, build_market_snapshot
from invest_ai.data.models import (
    DataContractError,
    Instrument,
    MarketBar,
    Quote,
    as_decimal,
    as_utc,
    canonical_json_bytes,
)


class MarketMonitorError(ValueError):
    """An observation is invalid, contradictory, or outside monitor scope."""


MarketObservation: TypeAlias = MarketBar | Quote


def _positive_decimal(value: object, name: str) -> Decimal:
    try:
        result = as_decimal(value, name)
    except DataContractError as exc:
        raise MarketMonitorError(str(exc)) from exc
    assert isinstance(result, Decimal)
    if result <= 0:
        raise MarketMonitorError(f"{name} must be greater than zero")
    return result


@dataclass(frozen=True, slots=True)
class MarketMonitorConfig:
    price_move_percent: Decimal | int | str = Decimal("3")
    volume_multiple: Decimal | int | str = Decimal("3")
    spread_multiple: Decimal | int | str = Decimal("3")
    max_spread_bps: Decimal | int | str = Decimal("100")
    top_book_imbalance: Decimal | int | str = Decimal("0.75")
    lookback: int = 5
    max_observation_age: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        for name in ("price_move_percent", "volume_multiple", "spread_multiple", "max_spread_bps"):
            value = _positive_decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
        imbalance = as_decimal(self.top_book_imbalance, "top_book_imbalance")
        assert isinstance(imbalance, Decimal)
        if not Decimal("0") < imbalance <= Decimal("1"):
            raise MarketMonitorError("top_book_imbalance must be in (0, 1]")
        object.__setattr__(self, "top_book_imbalance", imbalance)
        if type(self.lookback) is not int or self.lookback < 3 or self.lookback > 500:
            raise MarketMonitorError("lookback must be an integer between 3 and 500")
        if not isinstance(self.max_observation_age, timedelta) or self.max_observation_age <= timedelta(0):
            raise MarketMonitorError("max_observation_age must be a positive timedelta")


@dataclass(frozen=True, slots=True)
class MarketAlert:
    alert_id: str
    rule_code: str
    severity: str
    instrument_id: str
    symbol: str
    direction: str | None
    metric: str
    observed_value: Decimal
    threshold_value: Decimal
    baseline_value: Decimal | None
    unit: str
    observed_at: datetime
    source_provider: str
    source_dataset: str
    observation_id: str
    market_snapshot_id: str
    explanation: str
    authority_status: str = "OBSERVATION_ONLY"


@dataclass(frozen=True, slots=True)
class MarketMonitorResult:
    instrument_id: str
    symbol: str
    data_kind: str
    source_provider: str
    source_dataset: str
    observed_at: datetime
    available_at: datetime
    observation_id: str
    observation_sha256: str
    market_snapshot_id: str
    price: Decimal
    currency: str
    stale: bool
    spread_bps: Decimal | None
    top_book_imbalance: Decimal | None
    book_scope: str
    volume: Decimal | None
    prior_volume_median: Decimal | None
    alerts: tuple[MarketAlert, ...]
    authority_status: str = "OBSERVATION_ONLY"
    trade_proposal: None = None


@dataclass(slots=True)
class _SeriesState:
    last_observed_at: datetime | None = None
    last_available_at: datetime | None = None
    prices: deque[Decimal] = field(default_factory=deque)
    volumes: deque[Decimal] = field(default_factory=deque)
    spreads: deque[Decimal] = field(default_factory=deque)


def _median(values: tuple[Decimal, ...] | list[Decimal] | deque[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _seconds(value: timedelta) -> Decimal:
    microseconds = value // timedelta(microseconds=1)
    return Decimal(microseconds) / Decimal("1000000")


class MarketAnomalyMonitor:
    """Replay-safe streaming detector with bounded per-source rolling windows."""

    def __init__(self, config: MarketMonitorConfig | None = None) -> None:
        self.config = config or MarketMonitorConfig()
        self._lock = threading.RLock()
        self._series: dict[tuple[str, ...], _SeriesState] = {}
        self._seen: OrderedDict[str, tuple[str, MarketMonitorResult]] = OrderedDict()
        self._seen_limit = max(1024, self.config.lookback * 20)

    def ingest(
        self,
        observation: MarketObservation,
        instrument: Instrument,
        *,
        as_of: datetime | str,
    ) -> MarketMonitorResult:
        if not isinstance(observation, (MarketBar, Quote)):
            raise MarketMonitorError("only canonical MarketBar or Quote observations are supported")
        if not isinstance(instrument, Instrument):
            raise MarketMonitorError("canonical Instrument metadata is required")
        try:
            cutoff = as_utc(as_of, "as_of")
            snapshot = build_market_snapshot(
                observation, instrument, as_of=cutoff,
                max_age=self.config.max_observation_age,
            )
        except DataContractError as exc:
            raise MarketMonitorError(f"observation cannot form a point-in-time snapshot: {exc}") from exc

        provenance = observation.provenance
        if isinstance(observation, MarketBar):
            if not observation.is_usable or observation.volume is None:
                raise MarketMonitorError("OHLCV bar requires open, high, low, close, and volume")
            if observation.adjusted is not False:
                raise MarketMonitorError("adjusted or unknown-adjustment bars are excluded from anomaly detection")
            if observation.bar_end > cutoff:
                raise MarketMonitorError("bar_end cannot be later than the monitoring cutoff")
            kind = f"BAR:{observation.interval}"
            volume = observation.volume
            spread_bps = None
            imbalance = None
            book_scope = "NONE"
        else:
            if not observation.is_complete or observation.is_crossed:
                raise MarketMonitorError("a complete, non-crossed two-sided quote is required")
            kind = "QUOTE"
            volume = None
            midpoint = snapshot.price
            spread_bps = (observation.ask - observation.bid) / midpoint * Decimal("10000")
            if observation.bid_size is not None and observation.ask_size is not None:
                total_size = observation.bid_size + observation.ask_size
                imbalance = ((observation.bid_size - observation.ask_size) / total_size
                             if total_size > 0 else None)
                book_scope = "TOP_OF_BOOK"
            else:
                imbalance = None
                book_scope = "QUOTE_ONLY"

        observation_digest = hashlib.sha256(canonical_json_bytes(observation)).hexdigest()
        instrument_metadata = tuple(
            str(getattr(instrument, name)) for name in instrument.__dataclass_fields__
        )
        metadata_bytes = "\x1f".join(instrument_metadata).encode("utf-8")
        input_digest = hashlib.sha256(
            canonical_json_bytes(observation) + b"\x00" + metadata_bytes
        ).hexdigest()
        observation_id = provenance.observation_id
        series_key = (instrument.instrument_id, kind, provenance.provider, provenance.dataset)

        with self._lock:
            prior_seen = self._seen.get(observation_id)
            if prior_seen is not None:
                if prior_seen[0] != input_digest:
                    raise MarketMonitorError("the same observation identity was replayed with different content")
                self._seen.move_to_end(observation_id)
                return prior_seen[1]

            state = self._series.setdefault(
                series_key,
                _SeriesState(
                    prices=deque(maxlen=self.config.lookback),
                    volumes=deque(maxlen=self.config.lookback),
                    spreads=deque(maxlen=self.config.lookback),
                ),
            )
            if state.last_observed_at is not None and provenance.observed_at <= state.last_observed_at:
                raise MarketMonitorError("out-of-order or corrected observations require a fresh replay window")
            if state.last_available_at is not None and provenance.available_at < state.last_available_at:
                raise MarketMonitorError("available_at moved backwards within the source stream")

            alerts: list[MarketAlert] = []
            prior_price = state.prices[-1] if state.prices else None
            prior_volume_median = _median(state.volumes) if len(state.volumes) >= self.config.lookback else None

            def emit(rule_code: str, severity: str, direction: str | None, metric: str,
                     observed_value: Decimal, threshold_value: Decimal,
                     baseline_value: Decimal | None, unit: str, explanation: str) -> None:
                alert_material = (
                    f"{observation_id}|{snapshot.snapshot_id}|{rule_code}|{observed_value}|"
                    f"{threshold_value}|{baseline_value}"
                )
                alert_id = hashlib.sha256(alert_material.encode("utf-8")).hexdigest()
                alerts.append(MarketAlert(
                    alert_id=alert_id, rule_code=rule_code, severity=severity,
                    instrument_id=instrument.instrument_id, symbol=instrument.symbol,
                    direction=direction, metric=metric, observed_value=observed_value,
                    threshold_value=threshold_value, baseline_value=baseline_value,
                    unit=unit, observed_at=provenance.observed_at,
                    source_provider=provenance.provider, source_dataset=provenance.dataset,
                    observation_id=observation_id, market_snapshot_id=snapshot.snapshot_id,
                    explanation=explanation,
                ))

            age = cutoff - provenance.observed_at
            if snapshot.stale:
                emit("STALE_OBSERVATION", "WARNING", None, "age_seconds",
                     _seconds(age),
                     _seconds(self.config.max_observation_age),
                     None, "seconds", "Observation trop ancienne : exclue des références et des alertes de marché.")
            else:
                if prior_price is not None:
                    move_percent = (snapshot.price - prior_price) / prior_price * Decimal("100")
                    if abs(move_percent) >= self.config.price_move_percent:
                        direction = "UP" if move_percent > 0 else "DOWN"
                        emit("PRICE_MOVE", "WARNING", direction, "absolute_return_percent",
                             abs(move_percent), self.config.price_move_percent,
                             prior_price, "percent",
                             "Variation inhabituelle entre deux observations consécutives; ce n’est pas un signal d’achat ou de vente.")

                if volume is not None and prior_volume_median is not None:
                    volume_threshold = prior_volume_median * self.config.volume_multiple
                    if volume >= volume_threshold and volume > 0:
                        emit("VOLUME_SPIKE", "WARNING", None, "volume",
                             volume, volume_threshold, prior_volume_median,
                             provenance.unit or "source_units",
                             "Volume supérieur au multiple configuré de la médiane des barres précédentes.")

                if spread_bps is not None:
                    prior_spread_median = _median(state.spreads) if len(state.spreads) >= self.config.lookback else None
                    absolute_high = spread_bps >= self.config.max_spread_bps
                    widened = (prior_spread_median is not None and prior_spread_median > 0
                               and spread_bps >= prior_spread_median * self.config.spread_multiple)
                    if absolute_high or widened:
                        threshold = (self.config.max_spread_bps if absolute_high
                                     else prior_spread_median * self.config.spread_multiple)
                        emit("SPREAD_WIDENED", "WARNING", None, "quoted_spread",
                             spread_bps, threshold, prior_spread_median, "basis_points",
                             "Écart bid/ask inhabituel; le calcul utilise uniquement la meilleure paire bid/ask disponible.")

                if imbalance is not None and abs(imbalance) >= self.config.top_book_imbalance:
                    direction = "BID_SIZE_HEAVIER" if imbalance > 0 else "ASK_SIZE_HEAVIER"
                    emit("TOP_OF_BOOK_IMBALANCE", "INFO", direction, "top_book_size_imbalance",
                         abs(imbalance), self.config.top_book_imbalance, None, "ratio",
                         "Déséquilibre des seules tailles au meilleur bid/ask; ne représente pas le carnet complet ni une pression prédictive.")

                state.prices.append(snapshot.price)
                if volume is not None:
                    state.volumes.append(volume)
                if spread_bps is not None:
                    state.spreads.append(spread_bps)
                state.last_observed_at = provenance.observed_at
                state.last_available_at = provenance.available_at

            result = MarketMonitorResult(
                instrument_id=instrument.instrument_id, symbol=instrument.symbol, data_kind=kind,
                source_provider=provenance.provider, source_dataset=provenance.dataset,
                observed_at=provenance.observed_at, available_at=provenance.available_at,
                observation_id=observation_id, observation_sha256=observation_digest,
                market_snapshot_id=snapshot.snapshot_id, price=snapshot.price,
                currency=snapshot.currency, stale=snapshot.stale,
                spread_bps=spread_bps, top_book_imbalance=imbalance, book_scope=book_scope,
                volume=volume, prior_volume_median=prior_volume_median,
                alerts=tuple(alerts),
            )
            self._seen[observation_id] = (input_digest, result)
            self._seen.move_to_end(observation_id)
            while len(self._seen) > self._seen_limit:
                self._seen.popitem(last=False)
            return result


__all__ = [
    "MarketAlert", "MarketAnomalyMonitor", "MarketMonitorConfig",
    "MarketMonitorError", "MarketMonitorResult",
]
