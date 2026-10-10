"""Immutable data contracts for the deterministic risk engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class AssetType(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    CRYPTO = "CRYPTO"
    BOND = "BOND"
    FOREX = "FOREX"
    OTHER = "OTHER"


class RiskResult(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class TradeProposal:
    """Untrusted proposal data; ACCEPT never means a real order was sent."""

    proposal_id: str
    idempotency_key: str
    instrument_id: str
    symbol: str
    asset_type: AssetType
    side: Side
    quantity: Decimal
    strategy_id: str
    created_at: datetime
    order_type: str = "MARKET"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    evidence_refs: tuple[str, ...] = ()
    market_snapshot_id: str = ""
    portfolio_snapshot_id: str = ""


@dataclass(frozen=True, slots=True)
class Position:
    """Portfolio position; market_value is denominated in portfolio base_currency."""
    instrument_id: str
    quantity: Decimal
    market_value: Decimal
    sector: str | None = None


@dataclass(frozen=True, slots=True)
class PendingExposure:
    """Reserved exposure in portfolio base_currency, including paper orders."""

    instrument_id: str
    notional: Decimal
    sector: str | None = None


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Portfolio cash/equity and all stored position values use base_currency."""
    snapshot_id: str
    as_of: datetime
    cash: Decimal
    equity: Decimal
    peak_equity: Decimal
    daily_pnl: Decimal
    base_currency: str
    positions: tuple[Position, ...] = ()
    pending_exposures: tuple[PendingExposure, ...] = ()
    recent_order_times: tuple[datetime, ...] = ()
    processed_proposal_ids: frozenset[str] = frozenset()
    processed_idempotency_keys: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class MarketState:
    """Risk-owned market contract; intentionally independent from invest_ai.data."""
    snapshot_id: str
    instrument_id: str
    observed_at: datetime
    price: Decimal
    market_open: bool | None
    currency: str
    venue: str
    multiplier: Decimal
    tick_size: Decimal | None = None
    lot_size: Decimal | None = None
    sector: str | None = None


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Versioned limits; monetary limits use PortfolioState.base_currency."""

    version: str
    allowed_instruments: frozenset[str]
    allowed_sides: frozenset[Side]
    allowed_asset_types: frozenset[AssetType]
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_concentration_ratio: Decimal
    max_sector_concentration_ratio: Decimal
    max_gross_exposure: Decimal
    min_cash_balance: Decimal
    max_loss_per_trade: Decimal
    max_daily_loss: Decimal
    max_drawdown_ratio: Decimal
    max_leverage: Decimal
    max_market_data_age_seconds: int
    max_portfolio_age_seconds: int
    max_orders_per_window: int
    frequency_window_seconds: int
    fee_rate: Decimal
    slippage_buffer_rate: Decimal
    require_stop_for_buy: bool = True


@dataclass(frozen=True, slots=True)
class RiskRuntimeState:
    """Trusted runtime facts which must not come from the model."""

    kill_switch_active: bool = False


@dataclass(frozen=True, slots=True)
class RiskViolation:
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision_id: str
    proposal_id: str
    policy_version: str
    timestamp: datetime
    result: RiskResult
    violations: tuple[RiskViolation, ...]
    calculated_metrics: tuple[tuple[str, str], ...]
    input_hash: str
    engine_version: str = "1"


@dataclass(frozen=True, slots=True)
class EvaluationInputs:
    """Internal helper used for stable canonical hashing."""

    proposal: TradeProposal = field(compare=False)
    portfolio: PortfolioState = field(compare=False)
    market: MarketState = field(compare=False)
    policy: RiskPolicy = field(compare=False)
    runtime: RiskRuntimeState = field(compare=False)
    evaluated_at: datetime = field(compare=False)
