"""Pure deterministic risk evaluation; deliberately contains no execution API."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import Enum
from typing import Any

from .models import (
    AssetType,
    EvaluationInputs,
    MarketState,
    PendingExposure,
    PortfolioState,
    Position,
    RiskDecision,
    RiskPolicy,
    RiskResult,
    RiskRuntimeState,
    RiskViolation,
    Side,
    TradeProposal,
)

ENGINE_VERSION = "2"
ZERO = Decimal("0")
ONE = Decimal("1")
MAX_DECIMAL_DIGITS = 50
MAX_DECIMAL_EXPONENT = 50
DECIMAL_CALCULATION_PRECISION = 200
_CURRENCY_RE = re.compile(r"\A[A-Z]{3}\Z", re.ASCII)


def _aligned_to_increment(value: Decimal, increment: Decimal) -> bool:
    """Check a tick/lot multiple without depending on the caller's context."""
    with localcontext() as context:
        context.prec = DECIMAL_CALCULATION_PRECISION
        return value % increment == ZERO


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluated_at must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        return str(value)
    if value == ZERO:
        return "0"
    sign, digits, exponent = value.as_tuple()
    if abs(exponent) > 200:
        return str(value)
    coefficient = "".join(str(digit) for digit in digits)
    prefix = "-" if sign else ""
    if exponent >= 0:
        return prefix + coefficient + ("0" * exponent)
    split = len(coefficient) + exponent
    if split > 0:
        whole, fraction = coefficient[:split], coefficient[split:]
    else:
        whole, fraction = "0", ("0" * -split) + coefficient
    fraction = fraction.rstrip("0")
    return prefix + whole + ("." + fraction if fraction else "")


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_text(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return {"$invalid_datetime": value.isoformat()}
        return {"$datetime": _timestamp(value)}
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _canonical(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, float):
        return {"$invalid_float": repr(value)}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return {"$unsupported": type(value).__name__}


def _hash_inputs(inputs: EvaluationInputs) -> str:
    encoded = json.dumps(_canonical(inputs), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_input_hash(
    proposal: TradeProposal,
    portfolio: PortfolioState,
    market: MarketState,
    policy: RiskPolicy,
    runtime: RiskRuntimeState,
    evaluated_at: datetime,
) -> str:
    """Hash exactly the canonical Risk evaluation inputs.

    ``evaluated_at`` is required, timezone-aware, normalized to UTC, and part
    of the digest. This is the public Risk-owned entry point; callers must not
    reproduce the canonicalization in another module.
    """
    now = _utc(evaluated_at)
    return _hash_inputs(EvaluationInputs(proposal, portfolio, market, policy, runtime, now))


def _is_decimal(value: object) -> bool:
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    sign, digits, exponent = value.as_tuple()
    return len(digits) <= MAX_DECIMAL_DIGITS and abs(exponent) <= MAX_DECIMAL_EXPONENT


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _evaluate(
    proposal: TradeProposal,
    portfolio: PortfolioState,
    market: MarketState,
    policy: RiskPolicy,
    runtime: RiskRuntimeState,
    evaluated_at: datetime,
) -> RiskDecision:
    """Return a reproducible risk verdict. ACCEPT is not an execution command.

    The caller supplies a timezone-aware evaluation timestamp. Given identical
    typed inputs, policy, runtime state and evaluation timestamp, this function
    returns the same decision ID, verdict, reason codes and metrics.
    """
    now = _utc(evaluated_at)
    input_hash = compute_input_hash(proposal, portfolio, market, policy, runtime, now)
    violations: list[RiskViolation] = []
    metrics: dict[str, str] = {}

    def reject(code: str, message: str) -> None:
        violations.append(RiskViolation(code, message, True))

    def review(code: str, message: str) -> None:
        violations.append(RiskViolation(code, message, False))

    def result() -> RiskDecision:
        if any(item.blocking for item in violations):
            verdict = RiskResult.REJECT
        elif violations:
            verdict = RiskResult.REVIEW_REQUIRED
        else:
            verdict = RiskResult.ACCEPT
        payload = {
            "input_hash": input_hash,
            "engine_version": ENGINE_VERSION,
            "result": verdict.value,
            "violations": [(item.code, item.blocking) for item in violations],
        }
        decision_id = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return RiskDecision(
            decision_id=decision_id,
            proposal_id=str(getattr(proposal, "proposal_id", "")),
            policy_version=str(getattr(policy, "version", "")),
            timestamp=now,
            result=verdict,
            violations=tuple(violations),
            calculated_metrics=tuple(sorted(metrics.items())),
            input_hash=input_hash,
            engine_version=ENGINE_VERSION,
        )

    # First validate trusted policy and critical input shape. Never calculate
    # with floats, NaN, infinities, naïve timestamps or unknown units.
    if not isinstance(proposal, TradeProposal):
        reject("INVALID_PROPOSAL", "Proposal must use the typed TradeProposal contract.")
        return result()
    if not isinstance(portfolio, PortfolioState) or not isinstance(market, MarketState):
        reject("INVALID_STATE", "Portfolio and market inputs must use typed snapshots.")
        return result()
    if not isinstance(policy, RiskPolicy) or not isinstance(runtime, RiskRuntimeState):
        reject("INVALID_POLICY_STATE", "Policy and runtime state must use trusted typed contracts.")
        return result()

    if not isinstance(policy.version, str) or not policy.version.strip():
        review("POLICY_VERSION_MISSING", "Risk policy version is missing.")
    if not isinstance(policy.allowed_instruments, frozenset) or not policy.allowed_instruments:
        review("ALLOWLIST_EMPTY", "No instruments are explicitly allowed.")
    if (
        not isinstance(policy.allowed_sides, frozenset)
        or not isinstance(policy.allowed_asset_types, frozenset)
        or not policy.allowed_sides
        or not policy.allowed_asset_types
    ):
        review("POLICY_ALLOWLIST_EMPTY", "Side and asset-type allowlists must be explicit.")

    positive_limits = (
        "max_order_notional",
        "max_position_notional",
        "max_gross_exposure",
        "max_loss_per_trade",
        "max_daily_loss",
        "max_leverage",
    )
    for name in positive_limits:
        value = getattr(policy, name)
        if not _is_decimal(value) or value <= ZERO:
            review("POLICY_LIMIT_INVALID", f"{name} must be a finite positive Decimal.")
    for name in ("max_concentration_ratio", "max_sector_concentration_ratio", "max_drawdown_ratio"):
        value = getattr(policy, name)
        if not _is_decimal(value) or value < ZERO or value > ONE:
            review("POLICY_RATIO_INVALID", f"{name} must be a finite Decimal between 0 and 1.")
    if not _is_decimal(policy.min_cash_balance) or policy.min_cash_balance < ZERO:
        review("POLICY_CASH_LIMIT_INVALID", "min_cash_balance must be a finite non-negative Decimal.")
    for name in ("fee_rate", "slippage_buffer_rate"):
        value = getattr(policy, name)
        if not _is_decimal(value) or value < ZERO:
            review("POLICY_COST_BUFFER_INVALID", f"{name} must be a finite non-negative Decimal.")
    if (
        isinstance(policy.max_market_data_age_seconds, bool)
        or not isinstance(policy.max_market_data_age_seconds, int)
        or policy.max_market_data_age_seconds <= 0
        or isinstance(policy.max_portfolio_age_seconds, bool)
        or not isinstance(policy.max_portfolio_age_seconds, int)
        or policy.max_portfolio_age_seconds <= 0
    ):
        review("POLICY_AGE_LIMIT_INVALID", "Market and portfolio freshness limits must be positive integers.")
    if (
        isinstance(policy.max_orders_per_window, bool)
        or not isinstance(policy.max_orders_per_window, int)
        or policy.max_orders_per_window <= 0
        or isinstance(policy.frequency_window_seconds, bool)
        or not isinstance(policy.frequency_window_seconds, int)
        or policy.frequency_window_seconds <= 0
    ):
        review("POLICY_FREQUENCY_INVALID", "Frequency limits must be positive integers.")

    # Invalid policy configuration is never interpreted using defaults. Some
    # fields are also used as divisors/window limits later, so stop here.
    if any(item.code.startswith("POLICY_") or item.code == "ALLOWLIST_EMPTY" for item in violations):
        return result()

    if runtime.kill_switch_active:
        reject("KILL_SWITCH_ACTIVE", "The global risk kill switch is active.")
    if not isinstance(runtime.kill_switch_active, bool):
        reject("KILL_SWITCH_STATE_INVALID", "Kill switch state is invalid.")

    required_text = (
        (proposal.proposal_id, "PROPOSAL_ID_MISSING"),
        (proposal.idempotency_key, "IDEMPOTENCY_KEY_MISSING"),
        (proposal.instrument_id, "INSTRUMENT_ID_MISSING"),
        (proposal.symbol, "SYMBOL_MISSING"),
        (proposal.strategy_id, "STRATEGY_ID_MISSING"),
        (proposal.market_snapshot_id, "MARKET_SNAPSHOT_MISSING"),
        (proposal.portfolio_snapshot_id, "PORTFOLIO_SNAPSHOT_MISSING"),
    )
    for value, code in required_text:
        if not isinstance(value, str) or not value.strip():
            review(code, "A required proposal identifier or snapshot reference is missing.")
    if (
        isinstance(proposal.proposal_id, str)
        and isinstance(portfolio.processed_proposal_ids, frozenset)
        and proposal.proposal_id in portfolio.processed_proposal_ids
    ):
        reject("DUPLICATE_PROPOSAL", "This proposal has already been processed.")
    if (
        isinstance(proposal.idempotency_key, str)
        and isinstance(portfolio.processed_idempotency_keys, frozenset)
        and proposal.idempotency_key in portfolio.processed_idempotency_keys
    ):
        reject("DUPLICATE_IDEMPOTENCY_KEY", "This idempotency key has already been processed.")

    if not isinstance(proposal.side, Side) or proposal.side not in policy.allowed_sides:
        reject("SIDE_NOT_ALLOWED", "Proposal side is invalid or not explicitly allowed.")
    if not isinstance(proposal.asset_type, AssetType) or proposal.asset_type not in policy.allowed_asset_types:
        reject("ASSET_TYPE_NOT_ALLOWED", "Asset type is invalid or not explicitly allowed.")
    if isinstance(proposal.instrument_id, str) and proposal.instrument_id not in policy.allowed_instruments:
        reject("INSTRUMENT_NOT_ALLOWED", "Instrument is not in the explicit allowlist.")
    if not isinstance(proposal.order_type, str) or proposal.order_type not in {"MARKET", "LIMIT"}:
        reject("ORDER_TYPE_UNSUPPORTED", "Only MARKET and LIMIT proposal types are supported by this evaluator.")
    if proposal.order_type == "LIMIT" and (not _is_decimal(proposal.limit_price) or proposal.limit_price <= ZERO):
        reject("LIMIT_PRICE_INVALID", "A LIMIT proposal requires a finite positive Decimal limit price.")
    if proposal.order_type == "MARKET" and proposal.limit_price is not None:
        reject("ORDER_FIELDS_INCOHERENT", "A MARKET proposal cannot include a limit price.")
    if not _is_decimal(proposal.quantity) or proposal.quantity <= ZERO:
        reject("QUANTITY_INVALID", "Quantity must be a finite positive Decimal.")

    if not isinstance(portfolio.base_currency, str) or not portfolio.base_currency.strip():
        reject("PORTFOLIO_BASE_CURRENCY_MISSING", "Portfolio base currency is required.")
    elif not _CURRENCY_RE.fullmatch(portfolio.base_currency):
        reject("PORTFOLIO_BASE_CURRENCY_INVALID", "Portfolio base currency must be a three-letter uppercase code.")
    if not isinstance(market.currency, str) or not market.currency.strip():
        reject("MARKET_CURRENCY_MISSING", "Market quote currency is required.")
    elif not _CURRENCY_RE.fullmatch(market.currency):
        reject("MARKET_CURRENCY_INVALID", "Market quote currency must be a three-letter uppercase code.")
    elif isinstance(portfolio.base_currency, str) and _CURRENCY_RE.fullmatch(portfolio.base_currency) and market.currency != portfolio.base_currency:
        reject("CURRENCY_MISMATCH_FX_REQUIRED", "Market and portfolio currencies differ; no trusted FX conversion is available.")

    if not isinstance(market.venue, str) or not market.venue.strip():
        reject("MARKET_VENUE_MISSING", "A market venue is required; no venue is inferred.")
    elif len(market.venue) > 128:
        reject("MARKET_VENUE_INVALID", "Market venue identifier exceeds the contract limit.")
    if not _is_decimal(market.multiplier) or market.multiplier <= ZERO:
        reject("MARKET_MULTIPLIER_INVALID", "Market multiplier must be a finite positive Decimal.")
    if market.tick_size is not None and (not _is_decimal(market.tick_size) or market.tick_size <= ZERO):
        reject("MARKET_TICK_SIZE_INVALID", "A supplied tick size must be a finite positive Decimal.")
    if market.lot_size is not None and (not _is_decimal(market.lot_size) or market.lot_size <= ZERO):
        reject("MARKET_LOT_SIZE_INVALID", "A supplied lot size must be a finite positive Decimal.")
    if proposal.stop_price is not None and (not _is_decimal(proposal.stop_price) or proposal.stop_price <= ZERO):
        reject("STOP_PRICE_INVALID", "A supplied stop price must be a finite positive Decimal.")

    if _is_decimal(market.tick_size) and market.tick_size > ZERO:
        if proposal.order_type == "LIMIT" and _is_decimal(proposal.limit_price) and not _aligned_to_increment(proposal.limit_price, market.tick_size):
            reject("LIMIT_PRICE_TICK_MISMATCH", "Limit price is not aligned to the market tick size.")
        if proposal.stop_price is not None and _is_decimal(proposal.stop_price) and not _aligned_to_increment(proposal.stop_price, market.tick_size):
            reject("STOP_PRICE_TICK_MISMATCH", "Stop price is not aligned to the market tick size.")
    if _is_decimal(market.lot_size) and market.lot_size > ZERO and _is_decimal(proposal.quantity):
        if not _aligned_to_increment(proposal.quantity, market.lot_size):
            reject("QUANTITY_LOT_MISMATCH", "Quantity is not aligned to the market lot size.")

    if not _aware(proposal.created_at):
        review("PROPOSAL_TIME_INVALID", "Proposal time must be timezone-aware.")
    elif proposal.created_at.astimezone(timezone.utc) > now:
        reject("PROPOSAL_FROM_FUTURE", "Proposal timestamp is later than the evaluation time.")

    if market.instrument_id != proposal.instrument_id:
        reject("MARKET_INSTRUMENT_MISMATCH", "Market snapshot instrument does not match proposal.")
    if market.snapshot_id != proposal.market_snapshot_id:
        reject("MARKET_SNAPSHOT_MISMATCH", "Market snapshot reference does not match proposal.")
    if portfolio.snapshot_id != proposal.portfolio_snapshot_id:
        reject("PORTFOLIO_SNAPSHOT_MISMATCH", "Portfolio snapshot reference does not match proposal.")
    if not _is_decimal(market.price) or market.price <= ZERO:
        reject("MARKET_PRICE_INVALID", "Reference price must be a finite positive Decimal.")
    if isinstance(market.currency, str) and _CURRENCY_RE.fullmatch(market.currency):
        metrics["market_currency"] = market.currency
    if isinstance(portfolio.base_currency, str) and _CURRENCY_RE.fullmatch(portfolio.base_currency):
        metrics["portfolio_base_currency"] = portfolio.base_currency
    if isinstance(market.venue, str) and market.venue.strip():
        metrics["market_venue"] = market.venue
    if _is_decimal(market.multiplier):
        metrics["contract_multiplier"] = _decimal_text(market.multiplier)
    if not _aware(market.observed_at):
        review("MARKET_TIMESTAMP_INVALID", "Market observation timestamp must be timezone-aware.")
    elif market.observed_at.astimezone(timezone.utc) > now:
        reject("MARKET_TIMESTAMP_FROM_FUTURE", "Market observation is later than the evaluation time.")
    elif isinstance(policy.max_market_data_age_seconds, int) and policy.max_market_data_age_seconds > 0:
        age = (now - market.observed_at.astimezone(timezone.utc)).total_seconds()
        metrics["market_data_age_seconds"] = str(age)
        if age > policy.max_market_data_age_seconds:
            reject("MARKET_DATA_STALE", "Market snapshot exceeds the configured freshness limit.")
    if market.market_open is False:
        reject("MARKET_CLOSED", "The market is closed for this proposal.")
    elif market.market_open is not True:
        review("MARKET_STATUS_UNKNOWN", "Market open/closed state is unknown.")

    if not _aware(portfolio.as_of):
        review("PORTFOLIO_TIMESTAMP_INVALID", "Portfolio snapshot timestamp must be timezone-aware.")
    elif portfolio.as_of.astimezone(timezone.utc) > now:
        reject("PORTFOLIO_TIMESTAMP_FROM_FUTURE", "Portfolio snapshot is later than the evaluation time.")
    elif isinstance(policy.max_portfolio_age_seconds, int) and policy.max_portfolio_age_seconds > 0:
        age = (now - portfolio.as_of.astimezone(timezone.utc)).total_seconds()
        metrics["portfolio_age_seconds"] = str(age)
        if age > policy.max_portfolio_age_seconds:
            reject("PORTFOLIO_STATE_STALE", "Portfolio snapshot exceeds the configured freshness limit.")

    for name in ("cash", "equity", "peak_equity", "daily_pnl"):
        value = getattr(portfolio, name)
        if not _is_decimal(value):
            review("PORTFOLIO_VALUE_INVALID", f"Portfolio {name} must be a finite Decimal.")
    if _is_decimal(portfolio.cash) and portfolio.cash < ZERO:
        reject("PORTFOLIO_CASH_NEGATIVE", "Portfolio cash cannot be negative.")
    if _is_decimal(portfolio.equity) and portfolio.equity <= ZERO:
        reject("PORTFOLIO_EQUITY_INVALID", "Portfolio equity must be positive.")
    if _is_decimal(portfolio.peak_equity) and portfolio.peak_equity <= ZERO:
        review("PORTFOLIO_PEAK_EQUITY_INVALID", "Peak equity must be positive to calculate drawdown.")
    if not isinstance(proposal.evidence_refs, tuple) or any(not isinstance(item, str) or not item.strip() for item in proposal.evidence_refs):
        review("EVIDENCE_REFERENCE_INVALID", "Evidence references must be a tuple of non-empty IDs.")

    if not isinstance(portfolio.positions, tuple) or not isinstance(portfolio.pending_exposures, tuple):
        review("PORTFOLIO_COLLECTION_INVALID", "Positions and pending exposures must be immutable tuples.")
    if (
        not isinstance(portfolio.recent_order_times, tuple)
        or not isinstance(portfolio.processed_proposal_ids, frozenset)
        or not isinstance(portfolio.processed_idempotency_keys, frozenset)
    ):
        review("PORTFOLIO_RUNTIME_COLLECTION_INVALID", "Order history and replay registries must be immutable collections.")
    if market.sector is not None and not isinstance(market.sector, str):
        review("MARKET_SECTOR_INVALID", "Market sector must be a string or absent.")
    if any(item.code in {"PORTFOLIO_COLLECTION_INVALID", "PORTFOLIO_RUNTIME_COLLECTION_INVALID"} for item in violations):
        return result()

    for position in portfolio.positions:
        if (
            not isinstance(position, Position)
            or
            not isinstance(position.instrument_id, str)
            or (position.sector is not None and not isinstance(position.sector, str))
            or not _is_decimal(position.quantity)
            or position.quantity < ZERO
            or not _is_decimal(position.market_value)
            or position.market_value < ZERO
        ):
            reject("POSITION_STATE_INVALID", "Portfolio position contains invalid identity, quantity or value.")
    for pending in portfolio.pending_exposures:
        if (
            not isinstance(pending, PendingExposure)
            or not isinstance(pending.instrument_id, str)
            or (pending.sector is not None and not isinstance(pending.sector, str))
            or not _is_decimal(pending.notional)
            or pending.notional < ZERO
        ):
            reject("PENDING_EXPOSURE_INVALID", "Pending exposure contains invalid identity or notional.")
    for timestamp in portfolio.recent_order_times:
        if not _aware(timestamp):
            review("ORDER_HISTORY_TIMESTAMP_INVALID", "Recent order timestamps must be timezone-aware.")
        elif timestamp.astimezone(timezone.utc) > now:
            reject("ORDER_HISTORY_FROM_FUTURE", "Order history contains a timestamp later than evaluation time.")

    # Critical shape/state failures cannot be converted into an approval by
    # subsequent metrics. Continue only when every required numeric is usable.
    if any(item.blocking for item in violations):
        return result()
    critical_numeric = (
        proposal.quantity,
        market.price,
        market.multiplier,
        portfolio.cash,
        portfolio.equity,
        portfolio.peak_equity,
        portfolio.daily_pnl,
    )
    if any(not _is_decimal(value) for value in critical_numeric):
        return result()
    if market.multiplier <= ZERO or market.currency != portfolio.base_currency:
        return result()
    if not isinstance(market.venue, str) or not market.venue.strip():
        return result()
    if portfolio.peak_equity <= ZERO:
        return result()
    if any(
        item.code in {
            "MARKET_TIMESTAMP_INVALID",
            "PORTFOLIO_TIMESTAMP_INVALID",
            "ORDER_HISTORY_TIMESTAMP_INVALID",
        }
        for item in violations
    ):
        return result()

    # Frequency and replay controls use persisted runtime/portfolio facts.
    active_window = [
        timestamp for timestamp in portfolio.recent_order_times
        if (now - timestamp.astimezone(timezone.utc)).total_seconds() <= policy.frequency_window_seconds
    ]
    metrics["orders_in_frequency_window"] = str(len(active_window))
    if len(active_window) >= policy.max_orders_per_window:
        reject("ORDER_FREQUENCY_LIMIT", "The configured order frequency limit is already reached.")

    # Revalidate the parameters needed for arithmetic; invalid policy already
    # creates a review outcome and must never be used as an implicit default.
    arithmetic_policy_values = (
        policy.max_order_notional,
        policy.max_position_notional,
        policy.max_concentration_ratio,
        policy.max_gross_exposure,
        policy.min_cash_balance,
        policy.max_loss_per_trade,
        policy.max_daily_loss,
        policy.max_drawdown_ratio,
        policy.max_leverage,
        policy.fee_rate,
        policy.slippage_buffer_rate,
    )
    if any(not _is_decimal(value) for value in arithmetic_policy_values):
        return result()

    # A buy limit caps the maximum price at which the proposed order can fill;
    # use that price for reservations and exposure instead of the last quote.
    risk_price = market.price
    if proposal.side == Side.BUY and proposal.order_type == "LIMIT":
        risk_price = proposal.limit_price
    order_notional = risk_price * proposal.quantity * market.multiplier
    market_notional = market.price * proposal.quantity * market.multiplier
    cost_buffer = order_notional * (policy.fee_rate + policy.slippage_buffer_rate)
    metrics["reference_price"] = _decimal_text(market.price)
    metrics["risk_price"] = _decimal_text(risk_price)
    metrics["order_notional"] = _decimal_text(order_notional)
    metrics["estimated_cost_buffer"] = _decimal_text(cost_buffer)

    if order_notional > policy.max_order_notional:
        reject("MAX_ORDER_NOTIONAL", "Order notional exceeds the configured maximum.")

    current_instrument_value = sum(
        (position.market_value for position in portfolio.positions if position.instrument_id == proposal.instrument_id), ZERO
    ) + sum(
        (item.notional for item in portfolio.pending_exposures if item.instrument_id == proposal.instrument_id), ZERO
    )
    current_instrument_quantity = sum(
        (position.quantity for position in portfolio.positions if position.instrument_id == proposal.instrument_id), ZERO
    )
    if proposal.side == Side.SELL and proposal.quantity > current_instrument_quantity:
        reject("SHORT_SELL_NOT_ALLOWED", "SELL quantity exceeds the held long position; short selling is not enabled.")

    position_delta = order_notional if proposal.side == Side.BUY else -market_notional
    resulting_position_value = max(ZERO, current_instrument_value + position_delta)
    metrics["position_value_after"] = _decimal_text(resulting_position_value)
    if resulting_position_value > policy.max_position_notional:
        reject("MAX_POSITION_NOTIONAL", "Resulting instrument position exceeds the configured maximum.")
    concentration = resulting_position_value / portfolio.equity
    metrics["instrument_concentration_after"] = _decimal_text(concentration)
    if concentration > policy.max_concentration_ratio:
        reject("MAX_INSTRUMENT_CONCENTRATION", "Resulting instrument concentration exceeds the configured maximum.")

    sector = market.sector
    if not sector:
        sector_values = {position.sector for position in portfolio.positions if position.instrument_id == proposal.instrument_id and position.sector}
        sector_values.update(item.sector for item in portfolio.pending_exposures if item.instrument_id == proposal.instrument_id and item.sector)
        if len(sector_values) == 1:
            sector = next(iter(sector_values))
    if not sector:
        review("SECTOR_UNKNOWN", "Sector exposure cannot be evaluated without instrument classification.")
    else:
        current_sector = sum(
            (position.market_value for position in portfolio.positions if position.sector == sector), ZERO
        ) + sum((item.notional for item in portfolio.pending_exposures if item.sector == sector), ZERO)
        sector_after = max(ZERO, current_sector + position_delta)
        sector_ratio = sector_after / portfolio.equity
        metrics["sector_exposure_after"] = _decimal_text(sector_after)
        metrics["sector_concentration_after"] = _decimal_text(sector_ratio)
        if sector_ratio > policy.max_sector_concentration_ratio:
            reject("MAX_SECTOR_CONCENTRATION", "Resulting sector concentration exceeds the configured maximum.")

    current_gross = sum((position.market_value for position in portfolio.positions), ZERO) + sum(
        (item.notional for item in portfolio.pending_exposures), ZERO
    )
    gross_after = max(ZERO, current_gross + position_delta)
    metrics["gross_exposure_after"] = _decimal_text(gross_after)
    if gross_after > policy.max_gross_exposure:
        reject("MAX_GROSS_EXPOSURE", "Resulting gross exposure exceeds the configured maximum.")
    leverage = gross_after / portfolio.equity
    metrics["leverage_after"] = _decimal_text(leverage)
    if leverage > policy.max_leverage:
        reject("MAX_LEVERAGE", "Resulting leverage exceeds the configured maximum.")

    cash_after = portfolio.cash - (order_notional + cost_buffer if proposal.side == Side.BUY else ZERO)
    metrics["cash_after_reservation"] = _decimal_text(cash_after)
    if proposal.side == Side.BUY and cash_after < policy.min_cash_balance:
        reject("INSUFFICIENT_CASH", "Available cash after conservative reservation is below the minimum.")

    risk_to_stop = ZERO
    if proposal.side == Side.BUY:
        if proposal.stop_price is None and policy.require_stop_for_buy:
            review("STOP_REQUIRED", "A buy without a stop cannot be assessed against the per-trade loss limit.")
        elif proposal.stop_price is None:
            review("TRADE_LOSS_UNBOUNDED", "No stop was supplied, so maximum loss per trade is not measurable.")
        elif not _is_decimal(proposal.stop_price) or proposal.stop_price <= ZERO or proposal.stop_price >= risk_price:
            reject("STOP_PRICE_INVALID", "BUY stop must be a finite positive price below the current reference price.")
        else:
            risk_to_stop = (risk_price - proposal.stop_price) * proposal.quantity * market.multiplier + cost_buffer
            metrics["estimated_loss_to_stop"] = _decimal_text(risk_to_stop)
    else:
        # Selling is allowed only to reduce an existing long position. The
        # evaluator does not infer short-side stop semantics or authorize it.
        risk_to_stop = cost_buffer
        metrics["estimated_loss_to_stop"] = _decimal_text(risk_to_stop)

    if risk_to_stop > policy.max_loss_per_trade:
        reject("MAX_LOSS_PER_TRADE", "Estimated loss to stop plus cost buffer exceeds the configured maximum.")

    daily_loss_so_far = max(ZERO, -portfolio.daily_pnl)
    projected_daily_loss = daily_loss_so_far + risk_to_stop
    metrics["projected_daily_loss"] = _decimal_text(projected_daily_loss)
    if projected_daily_loss > policy.max_daily_loss:
        reject("MAX_DAILY_LOSS", "Current daily loss plus proposed risk exceeds the configured maximum.")

    current_drawdown = max(ZERO, (portfolio.peak_equity - portfolio.equity) / portfolio.peak_equity)
    projected_drawdown = max(ZERO, (portfolio.peak_equity - (portfolio.equity - risk_to_stop)) / portfolio.peak_equity)
    metrics["current_drawdown_ratio"] = _decimal_text(current_drawdown)
    metrics["projected_drawdown_ratio"] = _decimal_text(projected_drawdown)
    if projected_drawdown > policy.max_drawdown_ratio:
        reject("MAX_DRAWDOWN", "Projected drawdown exceeds the configured maximum.")

    return result()


def evaluate(
    proposal: TradeProposal,
    portfolio: PortfolioState,
    market: MarketState,
    policy: RiskPolicy,
    runtime: RiskRuntimeState,
    evaluated_at: datetime,
) -> RiskDecision:
    """Evaluate with a fixed Decimal context, isolated from application code."""
    with localcontext() as context:
        context.prec = DECIMAL_CALCULATION_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return _evaluate(proposal, portfolio, market, policy, runtime, evaluated_at)
