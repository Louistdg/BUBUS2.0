from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from invest_ai.execution import (
    ExecutionContractError,
    ExecutionMode,
    ExecutionStatus,
    OrderType,
    PaperExecutionPolicy,
    PaperExecutionRequest,
    PaperMarketSnapshot,
    compute_execution_input_hash,
    simulate_paper_execution,
)


NOW = datetime(2025, 1, 3, 15, 0, tzinfo=timezone.utc)
DIGEST = "a" * 64


def request(*, side: str = "BUY", order_type: OrderType = OrderType.MARKET,
            limit_price: Decimal | None = None, quantity: Decimal = Decimal("2"),
            requested_at: datetime = NOW) -> PaperExecutionRequest:
    return PaperExecutionRequest(
        request_id="request-1", proposal_id="proposal-1", proposal_hash=DIGEST,
        risk_decision_hash="b" * 64, policy_decision_hash="c" * 64,
        instrument_id="instrument-1", symbol="TEST", side=side, quantity=quantity,
        order_type=order_type, limit_price=limit_price, market_snapshot_id="market-1",
        requested_at=requested_at,
    )


def market(*, snapshot_id: str = "market-1", instrument_id: str = "instrument-1",
           price: Decimal = Decimal("100"), observed_at: datetime = NOW) -> PaperMarketSnapshot:
    return PaperMarketSnapshot(snapshot_id, instrument_id, price, "EUR", Decimal("1"), observed_at)


def policy(*, fee: Decimal = Decimal("0.01"), slippage: Decimal = Decimal("10")) -> PaperExecutionPolicy:
    return PaperExecutionPolicy("paper-policy-v0", fee, slippage, "adverse-reference-v0")


class PaperExecutionTests(unittest.TestCase):
    def test_market_buy_is_deterministic_with_explicit_fee_and_slippage(self) -> None:
        result = simulate_paper_execution(request(), market(), policy())
        self.assertEqual(result.status, ExecutionStatus.FILLED)
        self.assertEqual(result.simulated_price, Decimal("100.10"))
        self.assertEqual(result.fee, Decimal("2.0020"))
        self.assertEqual(result.slippage_cost, Decimal("0.20"))
        self.assertEqual(result.filled_quantity, Decimal("2"))
        self.assertEqual(result.fill.simulated_at, NOW)

    def test_market_sell_applies_adverse_slippage_downward(self) -> None:
        result = simulate_paper_execution(request(side="SELL"), market(), policy())
        self.assertEqual(result.status, ExecutionStatus.FILLED)
        self.assertEqual(result.simulated_price, Decimal("99.90"))

    def test_limit_buy_fills_when_adjusted_price_meets_limit(self) -> None:
        result = simulate_paper_execution(
            request(order_type=OrderType.LIMIT, limit_price=Decimal("100.10")), market(), policy()
        )
        self.assertEqual(result.status, ExecutionStatus.FILLED)

    def test_limit_sell_fills_when_adjusted_price_meets_limit(self) -> None:
        result = simulate_paper_execution(
            request(side="SELL", order_type=OrderType.LIMIT, limit_price=Decimal("99.90")), market(), policy()
        )
        self.assertEqual(result.status, ExecutionStatus.FILLED)

    def test_limit_not_reached_is_not_filled(self) -> None:
        result = simulate_paper_execution(
            request(order_type=OrderType.LIMIT, limit_price=Decimal("100")), market(), policy()
        )
        self.assertEqual(result.status, ExecutionStatus.NOT_FILLED)
        self.assertEqual(result.reason_codes, ("LIMIT_PRICE_NOT_REACHED",))
        self.assertIsNone(result.fill)
        self.assertEqual(result.fee, Decimal("0"))

    def test_fee_and_slippage_can_be_explicitly_zero(self) -> None:
        result = simulate_paper_execution(request(), market(), policy(fee=Decimal("0"), slippage=Decimal("0")))
        self.assertEqual(result.simulated_price, Decimal("100"))
        self.assertEqual(result.fee, Decimal("0"))
        self.assertEqual(result.slippage_cost, Decimal("0"))

    def test_input_hash_covers_market_and_policy_inputs(self) -> None:
        base = compute_execution_input_hash(request(), market(), policy())
        self.assertNotEqual(base, compute_execution_input_hash(request(quantity=Decimal("3")), market(), policy()))
        self.assertNotEqual(base, compute_execution_input_hash(request(), market(price=Decimal("101")), policy()))
        self.assertNotEqual(base, compute_execution_input_hash(request(), market(), policy(fee=Decimal("0.02"))))

    def test_input_hash_canonicalizes_numerically_equal_decimals(self) -> None:
        first = compute_execution_input_hash(request(), market(), policy(fee=Decimal("0.010")))
        second = compute_execution_input_hash(request(), market(), policy(fee=Decimal("0.01")))
        self.assertEqual(first, second)

    def test_same_inputs_have_same_hash_and_result(self) -> None:
        inputs = (request(), market(), policy())
        first = simulate_paper_execution(*inputs)
        second = simulate_paper_execution(*inputs)
        self.assertEqual(first, second)
        self.assertEqual(first.input_hash, second.input_hash)

    def test_invalid_quantity_is_rejected_at_contract_boundary(self) -> None:
        with self.assertRaises(ExecutionContractError):
            request(quantity=Decimal("0"))

    def test_float_price_or_fee_is_rejected(self) -> None:
        with self.assertRaises(ExecutionContractError):
            market(price=100.0)  # type: ignore[arg-type]
        with self.assertRaises(ExecutionContractError):
            policy(fee=0.01)  # type: ignore[arg-type]

    def test_market_limit_shape_is_validated(self) -> None:
        with self.assertRaises(ExecutionContractError):
            request(order_type=OrderType.MARKET, limit_price=Decimal("100"))
        with self.assertRaises(ExecutionContractError):
            request(order_type=OrderType.LIMIT, limit_price=None)

    def test_live_mode_is_not_constructible(self) -> None:
        with self.assertRaises(ValueError):
            ExecutionMode("LIVE")
        with self.assertRaises(ExecutionContractError):
            replace(request(), mode="LIVE")  # type: ignore[arg-type]

    def test_mismatched_snapshot_or_instrument_is_rejected(self) -> None:
        mismatch = simulate_paper_execution(request(), market(snapshot_id="other"), policy())
        self.assertEqual(mismatch.status, ExecutionStatus.REJECTED)
        self.assertIn("MARKET_SNAPSHOT_MISMATCH", mismatch.reason_codes)
        wrong_instrument = simulate_paper_execution(request(), market(instrument_id="other"), policy())
        self.assertEqual(wrong_instrument.status, ExecutionStatus.REJECTED)
        self.assertIn("INSTRUMENT_MISMATCH", wrong_instrument.reason_codes)

    def test_future_market_snapshot_is_rejected(self) -> None:
        future = datetime(2025, 1, 3, 15, 0, 1, tzinfo=timezone.utc)
        result = simulate_paper_execution(request(), market(observed_at=future), policy())
        self.assertEqual(result.status, ExecutionStatus.REJECTED)
        self.assertIn("MARKET_DATA_AFTER_REQUEST", result.reason_codes)

    def test_paper_mode_constant_and_timestamps_are_enforced(self) -> None:
        self.assertIs(request().mode, ExecutionMode.PAPER)
        with self.assertRaises(ExecutionContractError):
            replace(request(), requested_at=datetime(2025, 1, 3))


if __name__ == "__main__":
    unittest.main()
