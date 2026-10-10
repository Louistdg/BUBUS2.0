"""Canonical mappings among DATA, RISK, and future POLICY inputs.

This module owns cross-module mappings. It does not evaluate risk or policy,
perform FX, start workers, or execute orders.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from invest_ai.data.market import MarketSnapshot
from invest_ai.policy import PolicyMode
from invest_ai.risk import ENGINE_VERSION, compute_input_hash
from invest_ai.risk.models import (
    AssetType,
    MarketState,
    PendingExposure,
    PortfolioState,
    Position,
    RiskDecision,
    RiskPolicy,
    RiskResult,
    RiskRuntimeState,
    Side,
    TradeProposal,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_MAX_DECIMAL_DIGITS = 50
_MAX_DECIMAL_EXPONENT = 50


class IntegrationContractError(ValueError):
    """A source contract cannot safely map to the requested target contract."""


class TrustedRiskAuthorityRequired(IntegrationContractError):
    """Policy input is unavailable until an authoritative Core receipt is verifiable."""


class CurrencyMismatchError(IntegrationContractError):
    """Market and portfolio units differ; this gateway does not perform FX."""


@dataclass(frozen=True, slots=True)
class MarketRiskMapping:
    """Risk state plus DATA-only provenance flags that RiskState cannot hold."""

    market_state: MarketState
    data_stale: bool
    data_as_of: datetime
    published_at: datetime | None
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class RiskInputBundle:
    """Unit-checked market and portfolio snapshots ready for Risk evaluation."""

    market: MarketRiskMapping
    portfolio: PortfolioState


@dataclass(frozen=True, slots=True)
class TrustedPortfolioSnapshot:
    """Explicit trusted portfolio boundary; monetary fields use base_currency.

    Every position market value and pending exposure notional must already be
    denominated in ``base_currency``. No currency is inferred or converted.
    """

    snapshot_id: str
    as_of: datetime
    cash: Decimal
    equity: Decimal
    peak_equity: Decimal
    daily_pnl: Decimal
    base_currency: str
    positions: tuple[Position, ...]
    pending_exposures: tuple[PendingExposure, ...]
    recent_order_times: tuple[datetime, ...]
    processed_proposal_ids: frozenset[str]
    processed_idempotency_keys: frozenset[str]

    def __post_init__(self) -> None:
        _required_text(self.snapshot_id, "snapshot_id")
        _aware_utc(self.as_of, "as_of")
        _require_currency(self.base_currency, "base_currency")
        for field_name in ("cash", "equity", "peak_equity", "daily_pnl"):
            _finite_decimal(getattr(self, field_name), field_name)
        if not isinstance(self.positions, tuple) or any(not isinstance(p, Position) for p in self.positions):
            raise IntegrationContractError("positions must be a tuple of Risk Position values.")
        if not isinstance(self.pending_exposures, tuple) or any(not isinstance(p, PendingExposure) for p in self.pending_exposures):
            raise IntegrationContractError("pending_exposures must be a tuple of Risk PendingExposure values.")
        for position in self.positions:
            _finite_decimal(position.quantity, "position.quantity")
            _finite_decimal(position.market_value, "position.market_value")
        for exposure in self.pending_exposures:
            _finite_decimal(exposure.notional, "pending_exposure.notional")
        if not isinstance(self.recent_order_times, tuple):
            raise IntegrationContractError("recent_order_times must be a tuple.")
        for timestamp in self.recent_order_times:
            _aware_utc(timestamp, "recent_order_time")
        if not isinstance(self.processed_proposal_ids, frozenset) or any(not isinstance(x, str) for x in self.processed_proposal_ids):
            raise IntegrationContractError("processed_proposal_ids must be a frozenset of strings.")
        if not isinstance(self.processed_idempotency_keys, frozenset) or any(not isinstance(x, str) for x in self.processed_idempotency_keys):
            raise IntegrationContractError("processed_idempotency_keys must be a frozenset of strings.")


@dataclass(frozen=True, slots=True)
class PolicyEvaluationInput:
    """Canonical, hashable input envelope for a future Policy Engine."""

    proposal_id: str
    proposal_hash: str
    risk_decision_id: str
    risk_input_hash: str
    risk_result: RiskResult
    risk_engine_version: str
    market_snapshot_id: str
    market_data_stale: bool
    portfolio_snapshot_id: str
    requested_mode: PolicyMode

    def __post_init__(self) -> None:
        for field_name in ("proposal_id", "risk_decision_id", "risk_engine_version", "market_snapshot_id", "portfolio_snapshot_id"):
            _required_text(getattr(self, field_name), field_name)
        if not isinstance(self.proposal_hash, str) or _SHA256.fullmatch(self.proposal_hash) is None:
            raise IntegrationContractError("proposal_hash must be lowercase SHA-256.")
        if not isinstance(self.risk_input_hash, str) or _SHA256.fullmatch(self.risk_input_hash) is None:
            raise IntegrationContractError("risk_input_hash must be lowercase SHA-256.")
        if self.risk_result is not RiskResult.ACCEPT:
            raise IntegrationContractError("Only an ACCEPT RiskDecision can form a PolicyEvaluationInput.")
        if self.requested_mode is not PolicyMode.PAPER:
            raise IntegrationContractError("PolicyEvaluationInput V0 supports PAPER only.")
        if not isinstance(self.market_data_stale, bool):
            raise IntegrationContractError("market_data_stale must be bool.")


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntegrationContractError(f"{name} is required.")
    return value


def _aware_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise IntegrationContractError(f"{name} must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _finite_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise IntegrationContractError(f"{name} must be a finite Decimal; floats and implicit conversions are forbidden.")
    digits = value.as_tuple().digits
    exponent = value.as_tuple().exponent
    if len(digits) > _MAX_DECIMAL_DIGITS or abs(exponent) > _MAX_DECIMAL_EXPONENT:
        raise IntegrationContractError(f"{name} exceeds the Risk contract Decimal bounds.")
    return value


def _require_currency(value: object, name: str) -> str:
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        raise IntegrationContractError(f"{name} must be an explicit uppercase three-letter currency code.")
    return value


def _validate_proposal_contract(proposal: TradeProposal) -> None:
    for field_name in ("proposal_id", "idempotency_key", "instrument_id", "symbol", "strategy_id",
                       "order_type", "market_snapshot_id", "portfolio_snapshot_id"):
        _required_text(getattr(proposal, field_name), f"proposal.{field_name}")
    if not isinstance(proposal.asset_type, AssetType) or not isinstance(proposal.side, Side):
        raise IntegrationContractError("proposal asset_type and side must use canonical enums.")
    _finite_decimal(proposal.quantity, "proposal.quantity")
    for field_name in ("limit_price", "stop_price"):
        value = getattr(proposal, field_name)
        if value is not None:
            _finite_decimal(value, f"proposal.{field_name}")
    _aware_utc(proposal.created_at, "proposal.created_at")
    if not isinstance(proposal.evidence_refs, tuple) or any(not isinstance(item, str) or not item.strip()
                                                            for item in proposal.evidence_refs):
        raise IntegrationContractError("proposal.evidence_refs must be an immutable tuple of non-empty strings.")


def _validate_risk_binding_types(
    proposal: TradeProposal,
    portfolio: PortfolioState,
    market: MarketState,
    policy: RiskPolicy,
    runtime: RiskRuntimeState,
    evaluated_at: datetime,
) -> None:
    """Reject malformed runtime values before the Risk-owned canonical hash.

    These are contract/type checks only. Financial eligibility remains owned
    by Risk.evaluate(); this prevents untyped JSON-like values from colliding
    with Risk's typed canonical representations.
    """
    _validate_proposal_contract(proposal)

    _required_text(portfolio.snapshot_id, "portfolio.snapshot_id")
    _aware_utc(portfolio.as_of, "portfolio.as_of")
    _require_currency(portfolio.base_currency, "portfolio.base_currency")
    for field_name in ("cash", "equity", "peak_equity", "daily_pnl"):
        _finite_decimal(getattr(portfolio, field_name), f"portfolio.{field_name}")
    if not isinstance(portfolio.positions, tuple) or any(not isinstance(item, Position) for item in portfolio.positions):
        raise IntegrationContractError("portfolio.positions must be a tuple of canonical Position values.")
    if not isinstance(portfolio.pending_exposures, tuple) or any(not isinstance(item, PendingExposure)
                                                                 for item in portfolio.pending_exposures):
        raise IntegrationContractError("portfolio.pending_exposures must be a tuple of canonical PendingExposure values.")
    for position in portfolio.positions:
        _required_text(position.instrument_id, "position.instrument_id")
        _finite_decimal(position.quantity, "position.quantity")
        _finite_decimal(position.market_value, "position.market_value")
        if position.sector is not None and not isinstance(position.sector, str):
            raise IntegrationContractError("position.sector must be a string or None.")
    for exposure in portfolio.pending_exposures:
        _required_text(exposure.instrument_id, "pending_exposure.instrument_id")
        _finite_decimal(exposure.notional, "pending_exposure.notional")
        if exposure.sector is not None and not isinstance(exposure.sector, str):
            raise IntegrationContractError("pending_exposure.sector must be a string or None.")
    if not isinstance(portfolio.recent_order_times, tuple):
        raise IntegrationContractError("portfolio.recent_order_times must be an immutable tuple.")
    for item in portfolio.recent_order_times:
        _aware_utc(item, "portfolio.recent_order_time")
    for field_name in ("processed_proposal_ids", "processed_idempotency_keys"):
        value = getattr(portfolio, field_name)
        if not isinstance(value, frozenset) or any(not isinstance(item, str) for item in value):
            raise IntegrationContractError(f"portfolio.{field_name} must be a frozenset of strings.")

    _required_text(market.snapshot_id, "market.snapshot_id")
    _required_text(market.instrument_id, "market.instrument_id")
    _aware_utc(market.observed_at, "market.observed_at")
    _finite_decimal(market.price, "market.price")
    if market.market_open is not None and not isinstance(market.market_open, bool):
        raise IntegrationContractError("market.market_open must be bool or None.")
    _require_currency(market.currency, "market.currency")
    _required_text(market.venue, "market.venue")
    _finite_decimal(market.multiplier, "market.multiplier")
    for field_name in ("tick_size", "lot_size"):
        value = getattr(market, field_name)
        if value is not None:
            _finite_decimal(value, f"market.{field_name}")
    if market.sector is not None and not isinstance(market.sector, str):
        raise IntegrationContractError("market.sector must be a string or None.")

    _required_text(policy.version, "risk_policy.version")
    if not isinstance(policy.allowed_instruments, frozenset) or any(not isinstance(item, str) for item in policy.allowed_instruments):
        raise IntegrationContractError("risk_policy.allowed_instruments must be a frozenset of strings.")
    if not isinstance(policy.allowed_sides, frozenset) or any(not isinstance(item, Side) for item in policy.allowed_sides):
        raise IntegrationContractError("risk_policy.allowed_sides must be a frozenset of canonical Side values.")
    if not isinstance(policy.allowed_asset_types, frozenset) or any(not isinstance(item, AssetType)
                                                                   for item in policy.allowed_asset_types):
        raise IntegrationContractError("risk_policy.allowed_asset_types must be a frozenset of canonical AssetType values.")
    for field_name in ("max_order_notional", "max_position_notional", "max_concentration_ratio",
                       "max_sector_concentration_ratio", "max_gross_exposure", "min_cash_balance",
                       "max_loss_per_trade", "max_daily_loss", "max_drawdown_ratio", "max_leverage",
                       "fee_rate", "slippage_buffer_rate"):
        _finite_decimal(getattr(policy, field_name), f"risk_policy.{field_name}")
    for field_name in ("max_market_data_age_seconds", "max_portfolio_age_seconds", "max_orders_per_window",
                       "frequency_window_seconds"):
        value = getattr(policy, field_name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise IntegrationContractError(f"risk_policy.{field_name} must be an integer.")
    if not isinstance(policy.require_stop_for_buy, bool):
        raise IntegrationContractError("risk_policy.require_stop_for_buy must be bool.")
    if not isinstance(runtime.kill_switch_active, bool):
        raise IntegrationContractError("risk_runtime.kill_switch_active must be bool.")
    _aware_utc(evaluated_at, "evaluated_at")


def _decimal_text(value: Decimal) -> str:
    _finite_decimal(value, "canonical Decimal")
    if value == 0:
        return "0"
    sign, digits, exponent = value.as_tuple()
    coefficient = "".join(str(digit) for digit in digits)
    while len(coefficient) > 1 and coefficient.endswith("0"):
        coefficient = coefficient[:-1]
        exponent += 1
    prefix = "-" if sign else ""
    if exponent >= 0:
        return prefix + coefficient + ("0" * exponent)
    split = len(coefficient) + exponent
    if split > 0:
        whole, fraction = coefficient[:split], coefficient[split:]
    else:
        whole, fraction = "0", ("0" * -split) + coefficient
    return prefix + whole + "." + fraction


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_text(value)}
    if isinstance(value, datetime):
        aware = _aware_utc(value, "timestamp")
        return {"$datetime": aware.isoformat(timespec="microseconds").replace("+00:00", "Z")}
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise IntegrationContractError("Canonical object keys must be strings.")
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, float):
        raise IntegrationContractError("Float values are forbidden in canonical hashes.")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise IntegrationContractError(f"Unsupported canonical type: {type(value).__name__}.")


def canonical_json(value: Any) -> bytes:
    """Return compact deterministic JSON bytes without repr or float coercion."""
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def map_market_snapshot_to_risk(
    snapshot: MarketSnapshot,
    *,
    evaluation_time: datetime,
    market_open: bool | None = None,
    sector: str | None = None,
) -> MarketRiskMapping:
    """Map a DATA snapshot without inventing market-open or sector facts.

    The returned wrapper retains DATA freshness and source timestamps because
    the independent Risk MarketState contract has no fields for those values.
    ``observed_at`` is passed through unchanged after the point-in-time check.
    """
    if not isinstance(snapshot, MarketSnapshot):
        raise TypeError("MarketSnapshot DATA contract required.")
    cutoff = _aware_utc(evaluation_time, "evaluation_time")
    if not isinstance(snapshot.stale, bool):
        raise IntegrationContractError("MarketSnapshot.stale must be bool.")
    _required_text(snapshot.instrument_id, "market.instrument_id")
    timestamps = {
        "snapshot.as_of": snapshot.as_of,
        "snapshot.observed_at": snapshot.observed_at,
        "snapshot.received_at": snapshot.received_at,
        "snapshot.available_at": snapshot.available_at,
    }
    if snapshot.published_at is not None:
        timestamps["snapshot.published_at"] = snapshot.published_at
    for name, timestamp in timestamps.items():
        if _aware_utc(timestamp, name) > cutoff:
            raise IntegrationContractError(f"{name} is later than evaluation_time; future market data is refused.")
    if market_open is not None and not isinstance(market_open, bool):
        raise IntegrationContractError("market_open must be bool or None from a trusted source.")
    if sector is not None and (not isinstance(sector, str) or not sector.strip()):
        raise IntegrationContractError("sector must be a non-empty trusted fact or None.")
    price = _finite_decimal(snapshot.price, "market.price")
    if price <= 0:
        raise IntegrationContractError("market.price must be positive.")
    multiplier = _finite_decimal(snapshot.multiplier, "market.multiplier")
    if multiplier <= 0:
        raise IntegrationContractError("market.multiplier must be positive.")
    tick_size = snapshot.tick_size
    if tick_size is not None and _finite_decimal(tick_size, "market.tick_size") <= 0:
        raise IntegrationContractError("market.tick_size must be positive when present.")
    lot_size = snapshot.lot_size
    if lot_size is not None and _finite_decimal(lot_size, "market.lot_size") <= 0:
        raise IntegrationContractError("market.lot_size must be positive when present.")
    _require_currency(snapshot.currency, "market.currency")
    _required_text(snapshot.venue, "market.venue")
    state = MarketState(
        snapshot_id=snapshot.snapshot_id,
        instrument_id=snapshot.instrument_id,
        observed_at=snapshot.observed_at,
        price=snapshot.price,
        market_open=market_open,
        currency=snapshot.currency,
        venue=snapshot.venue,
        multiplier=multiplier,
        tick_size=tick_size,
        lot_size=lot_size,
        sector=sector,
    )
    return MarketRiskMapping(state, snapshot.stale, snapshot.as_of, snapshot.published_at,
                             snapshot.received_at, snapshot.available_at)


def build_risk_portfolio_state(snapshot: TrustedPortfolioSnapshot) -> PortfolioState:
    """Map an explicit trusted portfolio snapshot; no currency default or FX."""
    if not isinstance(snapshot, TrustedPortfolioSnapshot):
        raise TypeError("TrustedPortfolioSnapshot with explicit base_currency required.")
    return PortfolioState(
        snapshot_id=snapshot.snapshot_id,
        as_of=_aware_utc(snapshot.as_of, "portfolio.as_of"),
        cash=_finite_decimal(snapshot.cash, "portfolio.cash"),
        equity=_finite_decimal(snapshot.equity, "portfolio.equity"),
        peak_equity=_finite_decimal(snapshot.peak_equity, "portfolio.peak_equity"),
        daily_pnl=_finite_decimal(snapshot.daily_pnl, "portfolio.daily_pnl"),
        base_currency=_require_currency(snapshot.base_currency, "portfolio.base_currency"),
        positions=snapshot.positions,
        pending_exposures=snapshot.pending_exposures,
        recent_order_times=tuple(_aware_utc(item, "recent_order_time") for item in snapshot.recent_order_times),
        processed_proposal_ids=snapshot.processed_proposal_ids,
        processed_idempotency_keys=snapshot.processed_idempotency_keys,
    )


def build_risk_input_bundle(
    market_snapshot: MarketSnapshot,
    portfolio_snapshot: TrustedPortfolioSnapshot,
    *,
    evaluation_time: datetime,
    market_open: bool | None = None,
    sector: str | None = None,
) -> RiskInputBundle:
    """Build both Risk snapshots and fail closed before evaluation on currency mismatch."""
    cutoff = _aware_utc(evaluation_time, "evaluation_time")
    mapped_market = map_market_snapshot_to_risk(
        market_snapshot, evaluation_time=cutoff, market_open=market_open, sector=sector,
    )
    mapped_portfolio = build_risk_portfolio_state(portfolio_snapshot)
    if mapped_portfolio.as_of > cutoff or any(item > cutoff for item in mapped_portfolio.recent_order_times):
        raise IntegrationContractError("Portfolio snapshot contains future information relative to evaluation_time.")
    if mapped_market.market_state.currency != mapped_portfolio.base_currency:
        raise CurrencyMismatchError(
            f"Market currency {mapped_market.market_state.currency} differs from portfolio base_currency "
            f"{mapped_portfolio.base_currency}; Risk evaluation requires same-currency inputs and FX is unsupported."
        )
    return RiskInputBundle(mapped_market, mapped_portfolio)


def compute_proposal_hash(proposal: TradeProposal) -> str:
    """Hash every TradeProposal field using canonical JSON, not Python repr."""
    if not isinstance(proposal, TradeProposal):
        raise TypeError("TradeProposal Risk contract required.")
    _validate_proposal_contract(proposal)
    return hashlib.sha256(canonical_json(proposal)).hexdigest()


def build_policy_evaluation_input(
    proposal: TradeProposal,
    risk_decision: RiskDecision,
    market_snapshot: MarketSnapshot,
    risk_inputs: RiskInputBundle,
    risk_policy: RiskPolicy,
    risk_runtime: RiskRuntimeState,
    evaluated_at: datetime,
    *,
    requested_mode: PolicyMode = PolicyMode.PAPER,
) -> PolicyEvaluationInput:
    """Validate caller values, then fail closed without a Core-issued receipt.

    The exact-input hash remains useful for detecting substitutions, but neither
    it nor a recomputed RiskDecision establishes its producer or authoritative
    kill-switch/policy state. Until Core can issue and verify a receipt from
    approved sources, this boundary must not create a PolicyEvaluationInput.
    """
    if not isinstance(proposal, TradeProposal):
        raise TypeError("TradeProposal Risk contract required.")
    if not isinstance(risk_decision, RiskDecision):
        raise TypeError("RiskDecision Risk contract required.")
    if not isinstance(risk_inputs, RiskInputBundle):
        raise TypeError("The exact RiskInputBundle used for Risk evaluation is required.")
    if not isinstance(risk_policy, RiskPolicy) or not isinstance(risk_runtime, RiskRuntimeState):
        raise TypeError("Canonical RiskPolicy and RiskRuntimeState are required.")
    _required_text(proposal.proposal_id, "proposal.proposal_id")
    _required_text(risk_decision.proposal_id, "risk_decision.proposal_id")
    if risk_decision.proposal_id != proposal.proposal_id:
        raise IntegrationContractError("RiskDecision.proposal_id does not match TradeProposal.proposal_id.")
    if not isinstance(risk_decision.result, RiskResult):
        raise IntegrationContractError("RiskDecision.result is not a canonical RiskResult.")
    if risk_decision.result is not RiskResult.ACCEPT:
        raise IntegrationContractError(f"Risk result {risk_decision.result.value} is not eligible for Policy input.")
    if not isinstance(market_snapshot, MarketSnapshot):
        raise TypeError("Canonical MarketSnapshot required.")
    if (not isinstance(risk_inputs.market, MarketRiskMapping)
            or not isinstance(risk_inputs.market.market_state, MarketState)
            or not isinstance(risk_inputs.portfolio, PortfolioState)):
        raise TypeError("RiskInputBundle must contain canonical MarketRiskMapping and PortfolioState values.")
    _validate_risk_binding_types(proposal, risk_inputs.portfolio, risk_inputs.market.market_state,
                                 risk_policy, risk_runtime, evaluated_at)
    risk_time = _aware_utc(evaluated_at, "evaluated_at")
    decision_time = _aware_utc(risk_decision.timestamp, "risk_decision.timestamp")
    if decision_time != risk_time:
        raise IntegrationContractError("evaluated_at does not match RiskDecision.timestamp.")
    supplied_market = map_market_snapshot_to_risk(
        market_snapshot,
        evaluation_time=risk_time,
        market_open=risk_inputs.market.market_state.market_open,
        sector=risk_inputs.market.market_state.sector,
    )
    if supplied_market != risk_inputs.market:
        raise IntegrationContractError("MarketSnapshot does not match the exact market input used by Risk.")
    portfolio = risk_inputs.portfolio
    if _aware_utc(portfolio.as_of, "portfolio.as_of") > risk_time:
        raise IntegrationContractError("Portfolio snapshot is later than RiskDecision.timestamp.")
    if any(_aware_utc(item, "recent_order_time") > risk_time for item in portfolio.recent_order_times):
        raise IntegrationContractError("Portfolio contains an order timestamp later than RiskDecision.timestamp.")
    if _aware_utc(proposal.created_at, "proposal.created_at") > risk_time:
        raise IntegrationContractError("TradeProposal timestamp is later than RiskDecision.timestamp.")
    _required_text(market_snapshot.snapshot_id, "market_snapshot.snapshot_id")
    _required_text(portfolio.snapshot_id, "portfolio.snapshot_id")
    market_currency = _require_currency(market_snapshot.currency, "market_snapshot.currency")
    if market_currency != _require_currency(portfolio.base_currency, "portfolio.base_currency"):
        raise CurrencyMismatchError(
            f"Market currency {market_snapshot.currency} differs from portfolio base_currency "
            f"{portfolio.base_currency}; FX conversion is not supported."
        )
    if proposal.market_snapshot_id != market_snapshot.snapshot_id:
        raise IntegrationContractError("TradeProposal market_snapshot_id does not match DATA snapshot.")
    if proposal.portfolio_snapshot_id != portfolio.snapshot_id:
        raise IntegrationContractError("TradeProposal portfolio_snapshot_id does not match PortfolioState.")
    if not isinstance(requested_mode, PolicyMode) or requested_mode is not PolicyMode.PAPER:
        raise IntegrationContractError("requested_mode must be canonical PolicyMode.PAPER.")
    if not isinstance(risk_decision.decision_id, str) or not risk_decision.decision_id.strip():
        raise IntegrationContractError("RiskDecision.decision_id is required.")
    if not isinstance(risk_decision.engine_version, str) or not risk_decision.engine_version.strip():
        raise IntegrationContractError("RiskDecision.engine_version is required.")
    if not isinstance(risk_decision.input_hash, str) or _SHA256.fullmatch(risk_decision.input_hash) is None:
        raise IntegrationContractError("RiskDecision.input_hash must be lowercase SHA-256.")
    _required_text(risk_decision.policy_version, "risk_decision.policy_version")
    if risk_decision.engine_version != ENGINE_VERSION:
        raise IntegrationContractError("RiskDecision.engine_version does not match the canonical Risk engine.")
    if risk_decision.policy_version != risk_policy.version:
        raise IntegrationContractError("RiskPolicy.version does not match RiskDecision.policy_version.")
    try:
        canonical_risk_hash = compute_input_hash(
            proposal,
            portfolio,
            risk_inputs.market.market_state,
            risk_policy,
            risk_runtime,
            risk_time,
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise IntegrationContractError(f"Risk inputs cannot be canonically hashed: {exc}") from exc
    if canonical_risk_hash != risk_decision.input_hash:
        raise IntegrationContractError("RiskDecision.input_hash does not match the exact supplied Risk inputs.")
    raise TrustedRiskAuthorityRequired(
        "Policy input refused: a coherent caller-supplied RiskDecision is not an authoritative verdict. "
        "The Orchestration Core has no operational approved policy, portfolio or market source, "
        "so no verifiable Risk receipt can authorize Policy."
    )


def compute_policy_input_hash(policy_input: PolicyEvaluationInput) -> str:
    """Hash the exact, immutable PolicyEvaluationInput; no clock is included."""
    if not isinstance(policy_input, PolicyEvaluationInput):
        raise TypeError("PolicyEvaluationInput required.")
    return hashlib.sha256(canonical_policy_input_bytes(policy_input)).hexdigest()


def canonical_policy_input_bytes(policy_input: PolicyEvaluationInput) -> bytes:
    """Expose the precise stable byte sequence used to compute ``input_hash``."""
    if not isinstance(policy_input, PolicyEvaluationInput):
        raise TypeError("PolicyEvaluationInput required.")
    return canonical_json(policy_input)
