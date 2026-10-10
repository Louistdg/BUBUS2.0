"""Pure deterministic V0 simulator. No persistence, network, or execution adapter."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from enum import Enum
from typing import Any

from invest_ai.execution.models import (
    ExecutionStatus,
    OrderType,
    PaperExecutionPolicy,
    PaperExecutionRequest,
    PaperExecutionResult,
    PaperFill,
    PaperMarketSnapshot,
)


@dataclass(frozen=True, slots=True)
class _SimulationInputs:
    request: PaperExecutionRequest
    market: PaperMarketSnapshot
    policy: PaperExecutionPolicy


def _decimal_text(value: Decimal) -> str:
    """Canonical numeric spelling without Decimal context or float conversion."""
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
    if hasattr(value, "isoformat"):
        return {"$datetime": value.isoformat(timespec="microseconds").replace("+00:00", "Z")}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Unsupported execution hash value: {type(value).__name__}")


def compute_execution_input_hash(
    request: PaperExecutionRequest,
    market: PaperMarketSnapshot,
    policy: PaperExecutionPolicy,
) -> str:
    """Hash all request, trusted fixture and fill-policy fields canonically."""
    payload = _canonical(asdict(_SimulationInputs(request, market, policy)))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def simulate_paper_execution(
    request: PaperExecutionRequest,
    market: PaperMarketSnapshot,
    policy: PaperExecutionPolicy,
) -> PaperExecutionResult:
    """Return a reproducible PAPER result from explicit immutable inputs only.

    V0 market convention: a market order is priced at reference_price adjusted
    adversely by slippage_bps (BUY up, SELL down). A limit order fills only if
    that same adjusted price satisfies its limit; it is never price-improved or
    filled from a condition that the supplied snapshot does not support. Fees
    are fee_rate times simulated notional. Timestamp is request.requested_at.
    """
    if not isinstance(request, PaperExecutionRequest):
        raise TypeError("PaperExecutionRequest required.")
    if not isinstance(market, PaperMarketSnapshot):
        raise TypeError("PaperMarketSnapshot required.")
    if not isinstance(policy, PaperExecutionPolicy):
        raise TypeError("PaperExecutionPolicy required.")
    input_hash = compute_execution_input_hash(request, market, policy)
    rejected: list[str] = []
    if request.market_snapshot_id != market.snapshot_id:
        rejected.append("MARKET_SNAPSHOT_MISMATCH")
    if request.instrument_id != market.instrument_id:
        rejected.append("INSTRUMENT_MISMATCH")
    if market.observed_at > request.requested_at:
        rejected.append("MARKET_DATA_AFTER_REQUEST")
    if rejected:
        return PaperExecutionResult(
            request, ExecutionStatus.REJECTED, policy.fill_model_version,
            market.reference_price, None, Decimal("0"), Decimal("0"), Decimal("0"),
            request.requested_at, tuple(rejected), input_hash, None,
        )

    with localcontext() as context:
        context.prec = 200
        slippage_rate = policy.slippage_bps / Decimal("10000")
        adjustment = Decimal("1") + slippage_rate if request.side == "BUY" else Decimal("1") - slippage_rate
        simulated_price = market.reference_price * adjustment
        if simulated_price <= 0:
            return PaperExecutionResult(
                request, ExecutionStatus.REJECTED, policy.fill_model_version,
                market.reference_price, None, Decimal("0"), Decimal("0"), Decimal("0"),
                request.requested_at, ("SLIPPAGE_PRICE_NONPOSITIVE",), input_hash, None,
            )

        if request.order_type is OrderType.LIMIT:
            assert request.limit_price is not None
            limit_satisfied = (
                simulated_price <= request.limit_price
                if request.side == "BUY"
                else simulated_price >= request.limit_price
            )
            if not limit_satisfied:
                return PaperExecutionResult(
                    request, ExecutionStatus.NOT_FILLED, policy.fill_model_version,
                    market.reference_price, None, Decimal("0"), Decimal("0"), Decimal("0"),
                    request.requested_at, ("LIMIT_PRICE_NOT_REACHED",), input_hash, None,
                )

        notional = simulated_price * request.quantity * market.multiplier
        fee = notional * policy.fee_rate
        slippage_cost = abs(simulated_price - market.reference_price) * request.quantity * market.multiplier
        fill = PaperFill(
            instrument_id=request.instrument_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            reference_price=market.reference_price,
            simulated_price=simulated_price,
            fee=fee,
            slippage_cost=slippage_cost,
            currency=market.currency,
            simulated_at=request.requested_at,
        )
        return PaperExecutionResult(
            request=request,
            status=ExecutionStatus.FILLED,
            fill_model_version=policy.fill_model_version,
            reference_price=market.reference_price,
            simulated_price=simulated_price,
            filled_quantity=request.quantity,
            fee=fee,
            slippage_cost=slippage_cost,
            simulated_at=request.requested_at,
            reason_codes=("PAPER_FILL_SIMULATED",),
            input_hash=input_hash,
            fill=fill,
        )


__all__ = ["compute_execution_input_hash", "simulate_paper_execution"]
