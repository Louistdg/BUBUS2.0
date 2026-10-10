"""Experimental volume-confirmed breakout candidates for offline research.

This module emits untrusted drafts only. It has no Risk, Policy, portfolio-write,
PAPER outbox, broker, or order-submission dependency.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import StrEnum
from statistics import median
from typing import Sequence

from invest_ai.data.market import MarketSnapshot, build_market_snapshot
from invest_ai.data.models import DataContractError, Instrument, MarketBar, Quote, as_decimal, as_utc


class StrategyContractError(ValueError):
    """Invalid policy or caller contract; no candidate is emitted."""


class SignalStatus(StrEnum):
    PROPOSAL_CANDIDATE = "PROPOSAL_CANDIDATE"
    SIGNAL_OBSERVED = "SIGNAL_OBSERVED"
    NO_SIGNAL = "NO_SIGNAL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class BreakoutPolicy:
    """Explicit illustrative thresholds; this is not an approved RiskPolicy."""

    policy_id: str = "BREAKOUT-RESEARCH-V0-FIXTURE"
    budget_currency: str = "USD"
    max_risk_per_trade: Decimal | str = Decimal("20")
    max_order_notional: Decimal | str = Decimal("1000")
    stop_loss_fraction: Decimal | str = Decimal("0.02")
    take_profit_fraction: Decimal | str = Decimal("0.04")
    relative_volume_multiple: Decimal | str = Decimal("2")
    max_spread_bps: Decimal | str = Decimal("20")
    lookback_bars: int = 3
    max_quote_age: timedelta = timedelta(seconds=5)
    max_bar_age: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise StrategyContractError("policy_id is required")
        if not isinstance(self.budget_currency, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,11}", self.budget_currency):
            raise StrategyContractError("budget_currency must be a valid currency or asset code")
        object.__setattr__(self, "budget_currency", self.budget_currency.upper())
        for name in (
            "max_risk_per_trade", "max_order_notional", "stop_loss_fraction",
            "take_profit_fraction", "relative_volume_multiple", "max_spread_bps",
        ):
            try:
                value = as_decimal(getattr(self, name), name)
            except DataContractError as exc:
                raise StrategyContractError(str(exc)) from exc
            assert isinstance(value, Decimal)
            if value <= 0:
                raise StrategyContractError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        if self.stop_loss_fraction >= Decimal("1") or self.take_profit_fraction >= Decimal("1"):
            raise StrategyContractError("stop and target fractions must be below 1")
        if type(self.lookback_bars) is not int or not 2 <= self.lookback_bars <= 250:
            raise StrategyContractError("lookback_bars must be an integer from 2 to 250")
        for name in ("max_quote_age", "max_bar_age"):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise StrategyContractError(f"{name} must be a positive timedelta")


@dataclass(frozen=True, slots=True)
class CandidateProposal:
    proposal_id: str
    strategy_id: str
    instrument_id: str
    symbol: str
    currency: str
    side: str
    quantity: Decimal
    order_type: str
    limit_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    estimated_loss_at_stop: Decimal
    estimated_reward_at_target: Decimal
    estimated_notional: Decimal
    reward_risk_ratio: Decimal
    rationale: str
    exit_conditions: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    market_snapshot_id: str
    portfolio_context_ref: str
    created_at: datetime
    authority_status: str = "RESEARCH_CANDIDATE_ONLY"
    risk_evaluation_status: str = "NOT_EVALUATED"
    execution_allowed: bool = False
    broker_order_sent: bool = False


@dataclass(frozen=True, slots=True)
class SignalEvaluation:
    status: SignalStatus
    candidate: CandidateProposal | None
    reason_codes: tuple[str, ...]
    metrics: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class BreakoutSignalObservation:
    """Live-data-only breakout observation; it contains no order or sizing."""

    status: SignalStatus
    direction: str | None
    symbol: str
    observed_at: datetime
    reason_codes: tuple[str, ...]
    metrics: tuple[tuple[str, str], ...]
    evidence_refs: tuple[str, ...]
    authority_status: str = "OBSERVATION_ONLY"
    execution_allowed: bool = False


def evaluate_breakout_signal(
    bars: Sequence[MarketBar],
    quote: Quote,
    instrument: Instrument,
    *,
    as_of: datetime,
    lookback_bars: int = 3,
    relative_volume_multiple: Decimal | str = Decimal("2"),
    max_spread_bps: Decimal | str = Decimal("20"),
    max_quote_age: timedelta = timedelta(seconds=5),
    max_signal_bar_age: timedelta = timedelta(minutes=5),
) -> BreakoutSignalObservation:
    """Observe a real breakout condition without constructing a trade order.

    Only Crypto.com real-time OHLCV and a current, complete top-of-book quote
    are accepted. Older lookback bars are expected to be historical; freshness
    applies to the latest closed signal bar and the quote.
    """
    def blocked(code: str) -> BreakoutSignalObservation:
        observed = as_of if isinstance(as_of, datetime) and as_of.tzinfo else datetime.now(timezone.utc)
        return BreakoutSignalObservation(SignalStatus.BLOCKED, None,
            instrument.symbol if isinstance(instrument, Instrument) else "", observed,
            (code,), (), ())

    if not isinstance(instrument, Instrument) or not isinstance(quote, Quote):
        return blocked("CANONICAL_INSTRUMENT_AND_QUOTE_REQUIRED")
    if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes)):
        return blocked("CANONICAL_BAR_SEQUENCE_REQUIRED")
    if type(lookback_bars) is not int or not 2 <= lookback_bars <= 250:
        return blocked("INVALID_LOOKBACK")
    if (not isinstance(max_quote_age, timedelta) or max_quote_age <= timedelta(0)
            or not isinstance(max_signal_bar_age, timedelta) or max_signal_bar_age <= timedelta(0)):
        return blocked("INVALID_FRESHNESS_POLICY")
    try:
        cutoff = as_utc(as_of, "as_of")
        volume_multiple_limit = as_decimal(relative_volume_multiple, "relative_volume_multiple")
        spread_limit = as_decimal(max_spread_bps, "max_spread_bps")
        assert isinstance(volume_multiple_limit, Decimal) and isinstance(spread_limit, Decimal)
        if volume_multiple_limit <= 0 or spread_limit <= 0:
            return blocked("INVALID_SIGNAL_THRESHOLDS")
        required = lookback_bars + 1
        if len(bars) < required:
            return blocked("INSUFFICIENT_HISTORY")
        selected = tuple(bars[-required:])
        if any(not isinstance(bar, MarketBar) for bar in selected):
            return blocked("CANONICAL_BARS_REQUIRED")
        if instrument.tick_size is None or instrument.lot_size is None:
            return blocked("TICK_AND_LOT_METADATA_REQUIRED")
        if (quote.provenance.provider != "crypto_com_exchange"
                or quote.provenance.dataset != "quote"
                or any(bar.provenance.provider != "crypto_com_exchange"
                       or bar.provenance.dataset != "market_bar" for bar in selected)):
            return blocked("CRYPTOCOM_REALTIME_PROVENANCE_REQUIRED")
        if (quote.provenance.instrument_id != instrument.instrument_id
                or any(bar.provenance.instrument_id != instrument.instrument_id for bar in selected)
                or quote.provenance.currency != instrument.currency
                or any(bar.provenance.currency != instrument.currency for bar in selected)):
            return blocked("INSTRUMENT_OR_CURRENCY_MISMATCH")
        if any(bar.adjusted is not False or not bar.is_usable or bar.volume is None for bar in selected):
            return blocked("BAR_OHLCV_OR_ADJUSTMENT_UNKNOWN")
        if len({bar.interval for bar in selected}) != 1:
            return blocked("MIXED_BAR_INTERVALS")
        if any(right.bar_end <= left.bar_end for left, right in zip(selected, selected[1:])):
            return blocked("OUT_OF_ORDER_OR_DUPLICATE_BARS")
        if any(left.bar_end != right.bar_start for left, right in zip(selected, selected[1:])):
            return blocked("GAPPED_OR_OVERLAPPING_BARS")
        if any(bar.bar_end > cutoff or bar.provenance.available_at > cutoff for bar in selected):
            return blocked("UNFINISHED_OR_FUTURE_BAR")
        if quote.provenance.observed_at < selected[-1].bar_end:
            return blocked("QUOTE_PRECEDES_SIGNAL_BAR")
        if (quote.provenance.received_at > cutoff or quote.provenance.available_at > cutoff
                or cutoff - quote.provenance.received_at > max_quote_age
                or cutoff - quote.provenance.available_at > max_quote_age):
            return blocked("STALE_QUOTE_DELIVERY")
        if quote.is_crossed or not quote.is_complete:
            return blocked("INCOMPLETE_OR_CROSSED_QUOTE")
        if quote.bid_size is None or quote.ask_size is None or quote.bid_size + quote.ask_size <= 0:
            return blocked("TOP_OF_BOOK_SIZES_REQUIRED")
        snapshots = tuple(build_market_snapshot(bar, instrument, as_of=cutoff,
                           max_age=max_signal_bar_age) for bar in selected)
        quote_snapshot = build_market_snapshot(quote, instrument, as_of=cutoff, max_age=max_quote_age)
        if snapshots[-1].stale or quote_snapshot.stale:
            return blocked("STALE_SIGNAL_BAR_OR_QUOTE")
        prior = selected[:-1]
        prior_high = max(bar.high for bar in prior if bar.high is not None)
        prior_low = min(bar.low for bar in prior if bar.low is not None)
        prior_median_volume = Decimal(median(bar.volume for bar in prior if bar.volume is not None))
        if prior_median_volume <= 0:
            return blocked("PRIOR_VOLUME_BASELINE_INVALID")
        current = selected[-1]
        assert current.close is not None and current.volume is not None
        volume_multiple = current.volume / prior_median_volume
        midpoint = quote_snapshot.price
        spread_bps = (quote.ask - quote.bid) / midpoint * Decimal("10000")
        imbalance = (quote.bid_size - quote.ask_size) / (quote.bid_size + quote.ask_size)
        metrics = (
            ("breakout_high", str(prior_high)),
            ("breakdown_low", str(prior_low)),
            ("relative_volume", str(volume_multiple)),
            ("spread_bps", str(spread_bps)),
            ("top_book_imbalance", str(imbalance)),
        )
        refs = tuple(snapshot.snapshot_id for snapshot in snapshots) + (quote_snapshot.snapshot_id,)
        if spread_bps > spread_limit:
            return BreakoutSignalObservation(SignalStatus.NO_SIGNAL, None, instrument.symbol, cutoff,
                ("SPREAD_ABOVE_CONFIGURED_LIMIT",), metrics, refs)
        if volume_multiple < volume_multiple_limit:
            return BreakoutSignalObservation(SignalStatus.NO_SIGNAL, None, instrument.symbol, cutoff,
                ("VOLUME_NOT_CONFIRMED",), metrics, refs)
        direction = None
        if current.close > prior_high and imbalance > 0:
            direction = "UP_BREAKOUT"
        elif current.close < prior_low and imbalance < 0:
            direction = "DOWN_BREAKDOWN"
        if direction is None:
            return BreakoutSignalObservation(SignalStatus.NO_SIGNAL, None, instrument.symbol, cutoff,
                ("BREAKOUT_OR_BOOK_CONFIRMATION_MISSING",), metrics, refs)
        return BreakoutSignalObservation(SignalStatus.SIGNAL_OBSERVED, direction, instrument.symbol,
            cutoff, (), metrics, refs)
    except (DataContractError, InvalidOperation, TypeError, ValueError, AssertionError):
        return blocked("MARKET_EVIDENCE_VALIDATION_FAILED")


def _blocked(code: str, metrics: tuple[tuple[str, str], ...] = ()) -> SignalEvaluation:
    return SignalEvaluation(SignalStatus.BLOCKED, None, (code,), metrics)


def _no_signal(code: str, metrics: tuple[tuple[str, str], ...] = ()) -> SignalEvaluation:
    return SignalEvaluation(SignalStatus.NO_SIGNAL, None, (code,), metrics)


def _floor_lot(quantity: Decimal, lot_size: Decimal) -> Decimal:
    lots = (quantity / lot_size).to_integral_value(rounding=ROUND_FLOOR)
    return lots * lot_size


def _round_tick(price: Decimal, tick: Decimal, *, rounding: str) -> Decimal:
    ticks = (price / tick).to_integral_value(rounding=rounding)
    return ticks * tick


def _snapshot_id(snapshot: MarketSnapshot) -> str:
    return snapshot.snapshot_id


def evaluate_breakout(
    bars: Sequence[MarketBar],
    quote: Quote,
    instrument: Instrument,
    *,
    position_quantity: Decimal | str,
    portfolio_context_ref: str,
    policy: BreakoutPolicy,
    market_data_status: str,
    as_of: datetime,
) -> SignalEvaluation:
    """Evaluate a simple rule and return an untrusted, non-executable draft.

    BUY requires a close above the prior lookback highs, volume above the prior
    median multiple, a narrow two-sided quote, and positive top-of-book
    imbalance. SELL mirrors this and requires an existing long position; short
    selling is never proposed. Sizing uses only the explicitly supplied
    illustrative budget and lot/tick metadata.
    """
    if not isinstance(policy, BreakoutPolicy):
        raise StrategyContractError("an explicit BreakoutPolicy is required")
    if not isinstance(instrument, Instrument) or not isinstance(quote, Quote):
        return _blocked("CANONICAL_INSTRUMENT_AND_QUOTE_REQUIRED")
    if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes)):
        return _blocked("CANONICAL_BAR_SEQUENCE_REQUIRED")
    try:
        cutoff = as_utc(as_of, "as_of")
        held_quantity = as_decimal(position_quantity, "position_quantity")
    except DataContractError:
        return _blocked("INVALID_TIME_OR_POSITION")
    if held_quantity < 0:
        return _blocked("SHORT_POSITION_UNSUPPORTED")
    if not isinstance(portfolio_context_ref, str) or not portfolio_context_ref.strip():
        return _blocked("PORTFOLIO_CONTEXT_REFERENCE_REQUIRED")
    if not isinstance(market_data_status, str) or market_data_status not in {"REALTIME", "SYNTHETIC_FIXTURE"}:
        return _blocked("MARKET_DATA_NOT_REALTIME_OR_SYNTHETIC_FIXTURE")
    required_count = policy.lookback_bars + 1
    if len(bars) < required_count:
        return _blocked("INSUFFICIENT_HISTORY", (("required_bars", str(required_count)),))
    selected = tuple(bars[-required_count:])
    if any(not isinstance(bar, MarketBar) for bar in selected):
        return _blocked("CANONICAL_BARS_REQUIRED")
    if instrument.currency != policy.budget_currency:
        return _blocked("CURRENCY_CONVERSION_NOT_CONFIGURED")
    if instrument.tick_size is None or instrument.lot_size is None:
        return _blocked("TICK_AND_LOT_METADATA_REQUIRED")

    try:
        snapshots = tuple(build_market_snapshot(
            bar, instrument, as_of=cutoff, max_age=policy.max_bar_age,
        ) for bar in selected)
        quote_snapshot = build_market_snapshot(
            quote, instrument, as_of=cutoff, max_age=policy.max_quote_age,
        )
    except DataContractError:
        return _blocked("POINT_IN_TIME_OR_INSTRUMENT_VALIDATION_FAILED")
    if (quote.provenance.received_at < quote.provenance.observed_at
            or quote.provenance.available_at < quote.provenance.observed_at):
        return _blocked("QUOTE_PROVENANCE_TIMESTAMPS_INCONSISTENT")
    # Prior lookback bars are expected to be older than the freshness window.
    # Freshness applies to the latest closed signal bar and the live quote only.
    if snapshots[-1].stale or quote_snapshot.stale:
        return _blocked("STALE_MARKET_DATA")
    if any(bar.bar_end > cutoff for bar in selected):
        return _blocked("BAR_NOT_CLOSED_AT_EVALUATION_TIME")
    if (cutoff - quote.provenance.received_at > policy.max_quote_age
            or cutoff - quote.provenance.available_at > policy.max_quote_age):
        return _blocked("STALE_QUOTE_DELIVERY")
    if quote.is_crossed or not quote.is_complete:
        return _blocked("INCOMPLETE_OR_CROSSED_QUOTE")
    if quote.provenance.dataset != "quote" or any(bar.provenance.dataset != "market_bar" for bar in selected):
        return _blocked("UNSUPPORTED_DATASET_TYPE")
    providers = {bar.provenance.provider for bar in selected}
    if len(providers) != 1 or quote.provenance.provider not in providers:
        return _blocked("MIXED_PROVIDER_HISTORY")
    if market_data_status == "SYNTHETIC_FIXTURE" and quote.provenance.provider != "SYNTHETIC_FIXTURE":
        return _blocked("SYNTHETIC_STATUS_PROVIDER_MISMATCH")
    if market_data_status == "REALTIME" and quote.provenance.provider not in {"IBKR", "crypto_com_exchange"}:
        return _blocked("REALTIME_STATUS_PROVIDER_MISMATCH")
    if any(bar.adjusted is not False or not bar.is_usable or bar.volume is None for bar in selected):
        return _blocked("BAR_ADJUSTMENT_OR_VOLUME_UNKNOWN")
    if any(bar.provenance.currency != instrument.currency for bar in selected):
        return _blocked("BAR_CURRENCY_MISMATCH")
    bar_ends = tuple(bar.bar_end for bar in selected)
    if any(right <= left for left, right in zip(bar_ends, bar_ends[1:])):
        return _blocked("OUT_OF_ORDER_OR_DUPLICATE_BARS")
    if len({bar.interval for bar in selected}) != 1:
        return _blocked("MIXED_BAR_INTERVALS")
    if any(left.bar_end != right.bar_start for left, right in zip(selected, selected[1:])):
        return _blocked("GAPPED_OR_OVERLAPPING_BARS")
    current_bar = selected[-1]
    if quote.provenance.observed_at < current_bar.bar_end:
        return _blocked("QUOTE_PRECEDES_SIGNAL_BAR")
    if quote.bid_size is None or quote.ask_size is None:
        return _blocked("TOP_OF_BOOK_SIZES_REQUIRED")
    if quote.bid_size + quote.ask_size <= 0:
        return _blocked("TOP_OF_BOOK_SIZES_INVALID")

    prior_bars = selected[:-1]
    prior_high = max(bar.high for bar in prior_bars if bar.high is not None)
    prior_low = min(bar.low for bar in prior_bars if bar.low is not None)
    prior_median_volume = Decimal(median(bar.volume for bar in prior_bars if bar.volume is not None))
    if prior_median_volume <= 0:
        return _blocked("PRIOR_VOLUME_BASELINE_INVALID")
    assert current_bar.close is not None and current_bar.volume is not None
    volume_multiple = current_bar.volume / prior_median_volume
    midpoint = quote_snapshot.price
    spread_bps = (quote.ask - quote.bid) / midpoint * Decimal("10000")  # type: ignore[operator]
    imbalance = (quote.bid_size - quote.ask_size) / (quote.bid_size + quote.ask_size)
    metrics = (
        ("breakout_high", str(prior_high)),
        ("breakdown_low", str(prior_low)),
        ("relative_volume", str(volume_multiple)),
        ("spread_bps", str(spread_bps)),
        ("top_book_imbalance", str(imbalance)),
    )
    if spread_bps > policy.max_spread_bps:
        return _no_signal("SPREAD_ABOVE_CONFIGURED_LIMIT", metrics)
    if volume_multiple < policy.relative_volume_multiple:
        return _no_signal("VOLUME_NOT_CONFIRMED", metrics)

    side: str | None = None
    if current_bar.close > prior_high and imbalance > 0:
        if held_quantity > 0:
            return _no_signal("EXISTING_POSITION_ADD_DISABLED", metrics)
        side = "BUY"
    elif current_bar.close < prior_low and imbalance < 0:
        if held_quantity <= 0:
            return _no_signal("NO_LONG_POSITION_TO_SELL", metrics)
        side = "SELL"
    else:
        return _no_signal("BREAKOUT_OR_BOOK_CONFIRMATION_MISSING", metrics)

    entry_price = quote.ask if side == "BUY" else quote.bid
    assert isinstance(entry_price, Decimal)
    tick = instrument.tick_size
    lot = instrument.lot_size
    assert tick is not None and lot is not None
    if entry_price % tick != 0:
        return _blocked("QUOTE_PRICE_OFF_TICK", metrics)
    if side == "BUY":
        stop = _round_tick(entry_price * (Decimal("1") - policy.stop_loss_fraction), tick, rounding=ROUND_FLOOR)
        target = _round_tick(entry_price * (Decimal("1") + policy.take_profit_fraction), tick, rounding=ROUND_CEILING)
    else:
        stop = _round_tick(entry_price * (Decimal("1") + policy.stop_loss_fraction), tick, rounding=ROUND_CEILING)
        target = _round_tick(entry_price * (Decimal("1") - policy.take_profit_fraction), tick, rounding=ROUND_FLOOR)
    if stop <= 0 or target <= 0:
        return _blocked("EXIT_PRICE_OUT_OF_RANGE", metrics)
    multiplier = instrument.multiplier
    risk_per_share = abs(entry_price - stop) * multiplier
    reward_per_share = abs(target - entry_price) * multiplier
    if risk_per_share <= 0 or reward_per_share <= 0:
        return _blocked("EXIT_DISTANCE_BELOW_TICK_RESOLUTION", metrics)
    quantity_by_risk = _floor_lot(policy.max_risk_per_trade / risk_per_share, lot)
    quantity_by_notional = _floor_lot(policy.max_order_notional / (entry_price * multiplier), lot)
    quantity = min(quantity_by_risk, quantity_by_notional)
    if side == "SELL":
        quantity = min(quantity, _floor_lot(held_quantity, lot))
    if quantity <= 0:
        return _no_signal("BUDGET_OR_POSITION_BELOW_ONE_LOT", metrics)

    estimated_loss = quantity * risk_per_share
    estimated_reward = quantity * reward_per_share
    notional = quantity * entry_price * multiplier
    ratio = reward_per_share / risk_per_share
    evidence_refs = tuple(_snapshot_id(snapshot) for snapshot in snapshots) + (quote_snapshot.snapshot_id,)
    identity_material = {
        "strategy": "BREAKOUT_VOLUME_TOP_BOOK_V0",
        "evidence": evidence_refs,
        "portfolio_context": portfolio_context_ref,
        "side": side,
        "quantity": str(quantity),
        "entry": str(entry_price),
        "stop": str(stop),
        "target": str(target),
        "policy": policy.policy_id,
    }
    proposal_id = hashlib.sha256(json.dumps(
        identity_material, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    rationale = (
        f"Clôture {current_bar.close} au-dessus du plus haut préalable {prior_high}. "
        if side == "BUY" else
        f"Clôture {current_bar.close} sous le plus bas préalable {prior_low}. "
    )
    rationale += (
        f"Volume {volume_multiple:.2f}× la médiane préalable; spread {spread_bps:.2f} bps; "
        f"déséquilibre top-of-book {imbalance:.3f}. Hypothèse de recherche, non validée pour la rentabilité."
    )
    exits = (
        f"Stop indicatif à {stop} {instrument.currency}.",
        f"Objectif indicatif à {target} {instrument.currency}.",
        "Le stop ne garantit pas le prix de sortie; frais, taxes, change, glissement, gaps et liquidité peuvent augmenter la perte.",
    )
    candidate = CandidateProposal(
        proposal_id=proposal_id, strategy_id="BREAKOUT_VOLUME_TOP_BOOK_V0",
        instrument_id=instrument.instrument_id, symbol=instrument.symbol,
        currency=instrument.currency, side=side, quantity=quantity,
        order_type="LIMIT", limit_price=entry_price, stop_price=stop,
        take_profit_price=target, estimated_loss_at_stop=estimated_loss,
        estimated_reward_at_target=estimated_reward, estimated_notional=notional,
        reward_risk_ratio=ratio, rationale=rationale, exit_conditions=exits,
        evidence_refs=evidence_refs, market_snapshot_id=quote_snapshot.snapshot_id,
        portfolio_context_ref=portfolio_context_ref, created_at=cutoff,
    )
    return SignalEvaluation(SignalStatus.PROPOSAL_CANDIDATE, candidate, (), metrics)


__all__ = [
    "BreakoutPolicy", "BreakoutSignalObservation", "CandidateProposal", "SignalEvaluation", "SignalStatus",
    "StrategyContractError", "evaluate_breakout", "evaluate_breakout_signal",
]
