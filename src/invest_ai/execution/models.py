"""Immutable, paper-only execution contracts for INVEST-AI."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ExecutionContractError(ValueError):
    """An execution input violates the explicit V0 contract."""


class ExecutionMode(StrEnum):
    PAPER = "PAPER"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class ExecutionStatus(StrEnum):
    FILLED = "FILLED"
    NOT_FILLED = "NOT_FILLED"
    REJECTED = "REJECTED"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionContractError(f"{name} must be a non-empty string.")
    return value


def _finite_decimal(value: object, name: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ExecutionContractError(f"{name} must be a finite Decimal; floats are forbidden.")
    digits = value.as_tuple().digits
    exponent = value.as_tuple().exponent
    if len(digits) > 50 or abs(exponent) > 50:
        raise ExecutionContractError(f"{name} exceeds the V0 Decimal bounds.")
    if positive and value <= 0:
        raise ExecutionContractError(f"{name} must be greater than zero.")
    if nonnegative and value < 0:
        raise ExecutionContractError(f"{name} must not be negative.")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionContractError(f"{name} must be a timezone-aware timestamp.")
    return value.astimezone(timezone.utc)


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ExecutionContractError(f"{name} must be a lowercase SHA-256 digest.")
    return value


@dataclass(frozen=True, slots=True)
class PaperExecutionRequest:
    request_id: str
    proposal_id: str
    proposal_hash: str
    risk_decision_hash: str
    policy_decision_hash: str
    instrument_id: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: OrderType
    limit_price: Decimal | None
    market_snapshot_id: str
    requested_at: datetime
    mode: ExecutionMode = ExecutionMode.PAPER

    def __post_init__(self) -> None:
        for name in ("request_id", "proposal_id", "instrument_id", "symbol", "market_snapshot_id"):
            _required_text(getattr(self, name), name)
        _sha256(self.proposal_hash, "proposal_hash")
        _sha256(self.risk_decision_hash, "risk_decision_hash")
        _sha256(self.policy_decision_hash, "policy_decision_hash")
        _finite_decimal(self.quantity, "quantity", positive=True)
        if not isinstance(self.order_type, OrderType):
            raise ExecutionContractError("order_type must be MARKET or LIMIT.")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ExecutionContractError("MARKET requests cannot include limit_price.")
        if self.order_type is OrderType.LIMIT:
            _finite_decimal(self.limit_price, "limit_price", positive=True)
        if self.side not in ("BUY", "SELL"):
            raise ExecutionContractError("side must be BUY or SELL.")
        if not isinstance(self.mode, ExecutionMode) or self.mode is not ExecutionMode.PAPER:
            raise ExecutionContractError("V0 supports PAPER mode only.")
        object.__setattr__(self, "requested_at", _utc(self.requested_at, "requested_at"))


@dataclass(frozen=True, slots=True)
class PaperMarketSnapshot:
    """Explicit caller-supplied price fixture; the simulator never fetches it."""

    snapshot_id: str
    instrument_id: str
    reference_price: Decimal
    currency: str
    multiplier: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        _required_text(self.snapshot_id, "snapshot_id")
        _required_text(self.instrument_id, "instrument_id")
        _finite_decimal(self.reference_price, "reference_price", positive=True)
        _finite_decimal(self.multiplier, "multiplier", positive=True)
        if not isinstance(self.currency, str) or re.fullmatch(r"[A-Z]{3}", self.currency) is None:
            raise ExecutionContractError("currency must be an uppercase three-letter code.")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))


@dataclass(frozen=True, slots=True)
class PaperExecutionPolicy:
    policy_version: str
    fee_rate: Decimal
    slippage_bps: Decimal
    fill_model_version: str
    mode: ExecutionMode = ExecutionMode.PAPER

    def __post_init__(self) -> None:
        _required_text(self.policy_version, "policy_version")
        _required_text(self.fill_model_version, "fill_model_version")
        _finite_decimal(self.fee_rate, "fee_rate", nonnegative=True)
        _finite_decimal(self.slippage_bps, "slippage_bps", nonnegative=True)
        if not isinstance(self.mode, ExecutionMode) or self.mode is not ExecutionMode.PAPER:
            raise ExecutionContractError("V0 supports PAPER mode only.")


@dataclass(frozen=True, slots=True)
class PaperFill:
    instrument_id: str
    symbol: str
    side: str
    quantity: Decimal
    reference_price: Decimal
    simulated_price: Decimal
    fee: Decimal
    slippage_cost: Decimal
    currency: str
    simulated_at: datetime


@dataclass(frozen=True, slots=True)
class PaperExecutionResult:
    request: PaperExecutionRequest
    status: ExecutionStatus
    fill_model_version: str
    reference_price: Decimal
    simulated_price: Decimal | None
    filled_quantity: Decimal
    fee: Decimal
    slippage_cost: Decimal
    simulated_at: datetime
    reason_codes: tuple[str, ...]
    input_hash: str
    fill: PaperFill | None


__all__ = [
    "ExecutionContractError", "ExecutionMode", "ExecutionStatus", "OrderType",
    "PaperExecutionPolicy", "PaperExecutionRequest", "PaperExecutionResult",
    "PaperFill", "PaperMarketSnapshot",
]
