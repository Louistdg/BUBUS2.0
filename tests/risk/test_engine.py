from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from invest_ai.risk import (
    AssetType,
    MarketState,
    PendingExposure,
    PortfolioState,
    Position,
    RiskPolicy,
    RiskResult,
    RiskRuntimeState,
    Side,
    TradeProposal,
    compute_input_hash,
    evaluate,
)

D = Decimal
UTC = timezone.utc
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)


def fixtures():
    proposal = TradeProposal(
        proposal_id="proposal-1",
        idempotency_key="strategy-1:AAPL:BUY:window-1",
        instrument_id="us-equity-aapl",
        symbol="AAPL",
        asset_type=AssetType.EQUITY,
        side=Side.BUY,
        quantity=D("2"),
        strategy_id="strategy-1",
        created_at=NOW - timedelta(seconds=1),
        stop_price=D("90"),
        evidence_refs=("observation-1",),
        market_snapshot_id="market-1",
        portfolio_snapshot_id="portfolio-1",
    )
    portfolio = PortfolioState(
        snapshot_id="portfolio-1",
        as_of=NOW - timedelta(seconds=1),
        cash=D("10000"),
        equity=D("10000"),
        peak_equity=D("10000"),
        daily_pnl=D("0"),
        base_currency="USD",
    )
    market = MarketState(
        snapshot_id="market-1",
        instrument_id="us-equity-aapl",
        observed_at=NOW - timedelta(seconds=2),
        price=D("100"),
        market_open=True,
        currency="USD",
        venue="XNYS",
        multiplier=D("1"),
        tick_size=None,
        lot_size=None,
        sector="technology",
    )
    policy = RiskPolicy(
        version="risk-v1",
        allowed_instruments=frozenset({"us-equity-aapl"}),
        allowed_sides=frozenset({Side.BUY, Side.SELL}),
        allowed_asset_types=frozenset({AssetType.EQUITY, AssetType.ETF}),
        max_order_notional=D("5000"),
        max_position_notional=D("5000"),
        max_concentration_ratio=D("0.50"),
        max_sector_concentration_ratio=D("0.50"),
        max_gross_exposure=D("20000"),
        min_cash_balance=D("100"),
        max_loss_per_trade=D("500"),
        max_daily_loss=D("1000"),
        max_drawdown_ratio=D("0.20"),
        max_leverage=D("2"),
        max_market_data_age_seconds=60,
        max_portfolio_age_seconds=60,
        max_orders_per_window=3,
        frequency_window_seconds=60,
        fee_rate=D("0.001"),
        slippage_buffer_rate=D("0.002"),
    )
    runtime = RiskRuntimeState()
    return proposal, portfolio, market, policy, runtime


def decide(*, proposal=None, portfolio=None, market=None, policy=None, runtime=None, at=NOW):
    defaults = fixtures()
    return evaluate(
        proposal if proposal is not None else defaults[0],
        portfolio if portfolio is not None else defaults[1],
        market if market is not None else defaults[2],
        policy if policy is not None else defaults[3],
        runtime if runtime is not None else defaults[4],
        at,
    )


class RiskEngineTests(unittest.TestCase):
    def test_accepts_safe_proposal_without_any_execution_side_effect(self):
        decision = decide()
        self.assertEqual(decision.result, RiskResult.ACCEPT)
        self.assertEqual(decision.proposal_id, "proposal-1")
        self.assertEqual(dict(decision.calculated_metrics)["order_notional"], "200")
        self.assertEqual(decision.timestamp, NOW)

    def test_same_typed_inputs_and_policy_produce_identical_decision(self):
        first = decide()
        second = decide()
        self.assertEqual(first, second)
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertEqual(first.input_hash, second.input_hash)

    def test_public_input_hash_exactly_matches_risk_decision(self):
        proposal, portfolio, market, policy, runtime = fixtures()
        expected = compute_input_hash(proposal, portfolio, market, policy, runtime, NOW)
        decision = evaluate(proposal, portfolio, market, policy, runtime, NOW)
        self.assertEqual(decision.input_hash, expected)

    def test_public_input_hash_is_stable_for_equivalent_utc_instants(self):
        proposal, portfolio, market, policy, runtime = fixtures()
        plus_three = timezone(timedelta(hours=3))
        self.assertEqual(
            compute_input_hash(proposal, portfolio, market, policy, runtime, NOW),
            compute_input_hash(proposal, portfolio, market, policy, runtime, NOW.astimezone(plus_three)),
        )

    def test_public_input_hash_changes_for_every_authoritative_input(self):
        proposal, portfolio, market, policy, runtime = fixtures()
        original = compute_input_hash(proposal, portfolio, market, policy, runtime, NOW)
        mutations = (
            (replace(proposal, quantity=D("3")), portfolio, market, policy, runtime, NOW),
            (replace(proposal, side=Side.SELL), portfolio, market, policy, runtime, NOW),
            (replace(proposal, order_type="LIMIT", limit_price=D("101")), portfolio, market, policy, runtime, NOW),
            (replace(proposal, stop_price=D("89")), portfolio, market, policy, runtime, NOW),
            (replace(proposal, market_snapshot_id="market-other"), portfolio, market, policy, runtime, NOW),
            (replace(proposal, portfolio_snapshot_id="portfolio-other"), portfolio, market, policy, runtime, NOW),
            (proposal, replace(portfolio, cash=D("9999")), market, policy, runtime, NOW),
            (proposal, portfolio, replace(market, price=D("101")), policy, runtime, NOW),
            (proposal, portfolio, replace(market, observed_at=NOW-timedelta(seconds=3)), policy, runtime, NOW),
            (proposal, portfolio, replace(market, currency="EUR"), policy, runtime, NOW),
            (proposal, portfolio, replace(market, multiplier=D("2")), policy, runtime, NOW),
            (proposal, portfolio, market, replace(policy, max_order_notional=D("4999")), runtime, NOW),
            (proposal, portfolio, market, policy, RiskRuntimeState(kill_switch_active=True), NOW),
            (proposal, portfolio, market, policy, runtime, NOW+timedelta(seconds=1)),
        )
        for values in mutations:
            with self.subTest(values=values):
                self.assertNotEqual(compute_input_hash(*values), original)

    def test_three_hour_market_data_is_rejected_from_risk_timestamp_and_policy_age(self):
        _, _, market, _, _ = fixtures()
        old_market = replace(market, observed_at=NOW-timedelta(hours=3))
        decision = decide(market=old_market, at=NOW)
        self.assertEqual(decision.result, RiskResult.REJECT)
        self.assertIn("MARKET_DATA_STALE", {item.code for item in decision.violations})

    def test_timezone_equivalent_timestamp_has_same_business_decision(self):
        proposal, portfolio, market, policy, runtime = fixtures()
        plus_one = timezone(timedelta(hours=1))
        decision = evaluate(proposal, portfolio, market, policy, runtime, NOW.astimezone(plus_one))
        self.assertEqual(decision, decide())

    def test_result_is_independent_of_global_decimal_precision(self):
        expected = decide()
        with localcontext() as context:
            context.prec = 4
            actual = decide()
        self.assertEqual(actual, expected)

    def test_naive_evaluation_time_is_rejected_at_boundary(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            decide(at=NOW.replace(tzinfo=None))

    def test_stale_market_data_is_rejected(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, observed_at=NOW - timedelta(seconds=61)))
        self.assertEqual(decision.result, RiskResult.REJECT)
        self.assertIn("MARKET_DATA_STALE", {v.code for v in decision.violations})

    def test_future_market_data_is_rejected(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, observed_at=NOW + timedelta(seconds=1)))
        self.assertIn("MARKET_TIMESTAMP_FROM_FUTURE", {v.code for v in decision.violations})

    def test_naive_market_timestamp_requires_review(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, observed_at=NOW.replace(tzinfo=None)))
        self.assertEqual(decision.result, RiskResult.REVIEW_REQUIRED)

    def test_invalid_and_non_finite_prices_are_rejected(self):
        _, _, market, _, _ = fixtures()
        for value in (D("0"), D("-1"), D("NaN"), D("Infinity"), 100.0):
            with self.subTest(value=value):
                decision = decide(market=replace(market, price=value))
                self.assertEqual(decision.result, RiskResult.REJECT)
                self.assertIn("MARKET_PRICE_INVALID", {v.code for v in decision.violations})

    def test_out_of_contract_decimal_magnitude_is_rejected(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, price=D("1e51")))
        self.assertIn("MARKET_PRICE_INVALID", {v.code for v in decision.violations})

    def test_invalid_quantity_is_rejected(self):
        proposal, *_ = fixtures()
        for value in (D("0"), D("-1"), D("NaN"), 2.0):
            with self.subTest(value=value):
                decision = decide(proposal=replace(proposal, quantity=value))
                self.assertIn("QUANTITY_INVALID", {v.code for v in decision.violations})

    def test_unknown_market_state_requires_review(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, market_open=None))
        self.assertEqual(decision.result, RiskResult.REVIEW_REQUIRED)
        self.assertIn("MARKET_STATUS_UNKNOWN", {v.code for v in decision.violations})

    def test_closed_market_is_rejected(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, market_open=False))
        self.assertIn("MARKET_CLOSED", {v.code for v in decision.violations})

    def test_snapshot_mismatch_is_rejected(self):
        proposal, *_ = fixtures()
        decision = decide(proposal=replace(proposal, market_snapshot_id="other"))
        self.assertIn("MARKET_SNAPSHOT_MISMATCH", {v.code for v in decision.violations})

    def test_unapproved_instrument_is_rejected(self):
        proposal, *_ = fixtures()
        decision = decide(proposal=replace(proposal, instrument_id="unknown"))
        self.assertIn("INSTRUMENT_NOT_ALLOWED", {v.code for v in decision.violations})

    def test_unapproved_asset_type_is_rejected(self):
        proposal, *_ = fixtures()
        decision = decide(proposal=replace(proposal, asset_type=AssetType.CRYPTO))
        self.assertIn("ASSET_TYPE_NOT_ALLOWED", {v.code for v in decision.violations})

    def test_incoherent_limit_order_is_rejected(self):
        proposal, *_ = fixtures()
        decision = decide(proposal=replace(proposal, order_type="LIMIT", limit_price=None))
        self.assertIn("LIMIT_PRICE_INVALID", {v.code for v in decision.violations})

    def test_unhashable_order_type_is_rejected_without_exception(self):
        proposal, *_ = fixtures()
        decision = decide(proposal=replace(proposal, order_type=[]))
        self.assertIn("ORDER_TYPE_UNSUPPORTED", {v.code for v in decision.violations})

    def test_buy_limit_reservation_uses_higher_limit_price(self):
        proposal, *_ = fixtures()
        proposal = replace(proposal, order_type="LIMIT", limit_price=D("110"))
        decision = decide(proposal=proposal)
        self.assertEqual(decision.result, RiskResult.ACCEPT)
        self.assertEqual(dict(decision.calculated_metrics)["order_notional"], "220")
        self.assertEqual(dict(decision.calculated_metrics)["position_value_after"], "220")

    def test_buy_limit_risk_price_is_used_for_position_cap(self):
        proposal, _, _, policy, _ = fixtures()
        proposal = replace(proposal, order_type="LIMIT", limit_price=D("110"))
        policy = replace(policy, max_position_notional=D("210"))
        decision = decide(proposal=proposal, policy=policy)
        self.assertIn("MAX_POSITION_NOTIONAL", {v.code for v in decision.violations})

    def test_buy_limit_below_quote_uses_limit_for_reserved_exposure(self):
        proposal, *_ = fixtures()
        proposal = replace(proposal, order_type="LIMIT", limit_price=D("90"), stop_price=D("80"))
        decision = decide(proposal=proposal)
        self.assertEqual(decision.result, RiskResult.ACCEPT)
        metrics = dict(decision.calculated_metrics)
        self.assertEqual(metrics["risk_price"], "90")
        self.assertEqual(metrics["order_notional"], "180")

    def test_market_multiplier_is_applied_to_order_notional(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, multiplier=D("3")))
        self.assertEqual(decision.result, RiskResult.ACCEPT)
        self.assertEqual(dict(decision.calculated_metrics)["order_notional"], "600")

    def test_market_multiplier_is_applied_to_cash_reservation(self):
        _, portfolio, market, policy, _ = fixtures()
        portfolio = replace(portfolio, cash=D("500"))
        market = replace(market, multiplier=D("2"))
        decision = decide(portfolio=portfolio, market=market, policy=policy)
        metrics = dict(decision.calculated_metrics)
        self.assertEqual(metrics["cash_after_reservation"], "98.8")
        self.assertIn("INSUFFICIENT_CASH", {item.code for item in decision.violations})

    def test_market_multiplier_is_applied_to_stop_loss(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, multiplier=D("2")))
        self.assertEqual(dict(decision.calculated_metrics)["estimated_loss_to_stop"], "41.2")

    def test_same_currency_allows_risk_calculation(self):
        self.assertEqual(decide().result, RiskResult.ACCEPT)

    def test_currency_mismatch_without_fx_fails_closed(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, currency="EUR"))
        self.assertEqual(decision.result, RiskResult.REJECT)
        self.assertIn("CURRENCY_MISMATCH_FX_REQUIRED", {item.code for item in decision.violations})
        self.assertNotIn("order_notional", dict(decision.calculated_metrics))

    def test_missing_currency_fails_closed(self):
        _, portfolio, market, _, _ = fixtures()
        for changed_market, changed_portfolio, expected in (
            (replace(market, currency=None), portfolio, "MARKET_CURRENCY_MISSING"),
            (market, replace(portfolio, base_currency=None), "PORTFOLIO_BASE_CURRENCY_MISSING"),
        ):
            with self.subTest(expected=expected):
                decision = decide(market=changed_market, portfolio=changed_portfolio)
                self.assertEqual(decision.result, RiskResult.REJECT)
                self.assertIn(expected, {item.code for item in decision.violations})

    def test_tick_aligned_limit_and_stop_are_accepted(self):
        proposal, _, market, _, _ = fixtures()
        proposal = replace(proposal, order_type="LIMIT", limit_price=D("100.25"), stop_price=D("90"))
        decision = decide(proposal=proposal, market=replace(market, tick_size=D("0.25")))
        self.assertEqual(decision.result, RiskResult.ACCEPT)

    def test_limit_price_off_tick_is_rejected(self):
        proposal, _, market, _, _ = fixtures()
        proposal = replace(proposal, order_type="LIMIT", limit_price=D("100.10"))
        decision = decide(proposal=proposal, market=replace(market, tick_size=D("0.25")))
        self.assertIn("LIMIT_PRICE_TICK_MISMATCH", {item.code for item in decision.violations})

    def test_stop_price_off_tick_is_rejected(self):
        proposal, _, market, _, _ = fixtures()
        proposal = replace(proposal, stop_price=D("90.10"))
        decision = decide(proposal=proposal, market=replace(market, tick_size=D("0.25")))
        self.assertIn("STOP_PRICE_TICK_MISMATCH", {item.code for item in decision.violations})

    def test_lot_aligned_quantity_is_accepted(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, lot_size=D("0.5")))
        self.assertEqual(decision.result, RiskResult.ACCEPT)

    def test_quantity_off_lot_is_rejected(self):
        proposal, _, market, _, _ = fixtures()
        proposal = replace(proposal, quantity=D("1.9"))
        decision = decide(proposal=proposal, market=replace(market, lot_size=D("0.5")))
        self.assertIn("QUANTITY_LOT_MISMATCH", {item.code for item in decision.violations})

    def test_missing_venue_fails_closed_without_inference(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, venue=""))
        self.assertEqual(decision.result, RiskResult.REJECT)
        self.assertIn("MARKET_VENUE_MISSING", {item.code for item in decision.violations})

    def test_market_contract_inputs_preserve_determinism(self):
        _, _, market, _, _ = fixtures()
        market = replace(market, multiplier=D("3"), tick_size=D("0.25"), lot_size=D("0.5"))
        self.assertEqual(decide(market=market), decide(market=market))

    def test_buy_stop_must_be_below_worst_case_limit_fill(self):
        proposal, *_ = fixtures()
        proposal = replace(proposal, order_type="LIMIT", limit_price=D("90"), stop_price=D("95"))
        decision = decide(proposal=proposal)
        self.assertIn("STOP_PRICE_INVALID", {v.code for v in decision.violations})

    def test_max_order_notional_is_enforced(self):
        proposal, _, _, policy, _ = fixtures()
        policy = replace(policy, max_order_notional=D("199"))
        decision = decide(proposal=replace(proposal, quantity=D("2")), policy=policy)
        self.assertIn("MAX_ORDER_NOTIONAL", {v.code for v in decision.violations})

    def test_max_position_and_concentration_are_enforced(self):
        proposal, _, _, policy, _ = fixtures()
        policy = replace(policy, max_position_notional=D("100"), max_concentration_ratio=D("0.01"))
        decision = decide(proposal=proposal, policy=policy)
        codes = {v.code for v in decision.violations}
        self.assertIn("MAX_POSITION_NOTIONAL", codes)
        self.assertIn("MAX_INSTRUMENT_CONCENTRATION", codes)

    def test_sector_concentration_is_enforced(self):
        proposal, portfolio, _, policy, _ = fixtures()
        portfolio = replace(
            portfolio,
            positions=(Position("other-tech", D("5"), D("5000"), "technology"),),
        )
        policy = replace(policy, max_sector_concentration_ratio=D("0.50"))
        decision = decide(proposal=proposal, portfolio=portfolio, policy=policy)
        self.assertIn("MAX_SECTOR_CONCENTRATION", {v.code for v in decision.violations})

    def test_missing_sector_requires_review(self):
        _, _, market, _, _ = fixtures()
        decision = decide(market=replace(market, sector=None))
        self.assertEqual(decision.result, RiskResult.REVIEW_REQUIRED)
        self.assertIn("SECTOR_UNKNOWN", {v.code for v in decision.violations})

    def test_cash_reservation_includes_configured_cost_buffer(self):
        proposal, portfolio, _, _, _ = fixtures()
        portfolio = replace(portfolio, cash=D("202"))
        decision = decide(proposal=proposal, portfolio=portfolio)
        self.assertIn("INSUFFICIENT_CASH", {v.code for v in decision.violations})

    def test_buy_without_stop_requires_review(self):
        proposal, *_ = fixtures()
        decision = decide(proposal=replace(proposal, stop_price=None))
        self.assertEqual(decision.result, RiskResult.REVIEW_REQUIRED)
        self.assertIn("STOP_REQUIRED", {v.code for v in decision.violations})

    def test_bad_buy_stop_is_rejected(self):
        proposal, *_ = fixtures()
        decision = decide(proposal=replace(proposal, stop_price=D("100")))
        self.assertIn("STOP_PRICE_INVALID", {v.code for v in decision.violations})

    def test_per_trade_loss_limit_is_enforced(self):
        proposal, _, _, policy, _ = fixtures()
        policy = replace(policy, max_loss_per_trade=D("1"))
        decision = decide(proposal=proposal, policy=policy)
        self.assertIn("MAX_LOSS_PER_TRADE", {v.code for v in decision.violations})

    def test_daily_loss_limit_includes_new_trade_risk(self):
        proposal, portfolio, _, policy, _ = fixtures()
        portfolio = replace(portfolio, daily_pnl=D("-990"))
        policy = replace(policy, max_daily_loss=D("1000"))
        decision = decide(proposal=proposal, portfolio=portfolio, policy=policy)
        self.assertIn("MAX_DAILY_LOSS", {v.code for v in decision.violations})

    def test_drawdown_limit_is_enforced(self):
        proposal, portfolio, _, policy, _ = fixtures()
        portfolio = replace(portfolio, equity=D("9000"), peak_equity=D("10000"))
        policy = replace(policy, max_drawdown_ratio=D("0.10"))
        decision = decide(proposal=proposal, portfolio=portfolio, policy=policy)
        self.assertIn("MAX_DRAWDOWN", {v.code for v in decision.violations})

    def test_gross_exposure_and_leverage_limits_are_enforced(self):
        proposal, _, _, policy, _ = fixtures()
        policy = replace(policy, max_gross_exposure=D("100"), max_leverage=D("0.01"))
        decision = decide(proposal=proposal, policy=policy)
        codes = {v.code for v in decision.violations}
        self.assertIn("MAX_GROSS_EXPOSURE", codes)
        self.assertIn("MAX_LEVERAGE", codes)

    def test_sell_must_not_exceed_existing_long_position(self):
        proposal, portfolio, _, _, _ = fixtures()
        proposal = replace(proposal, side=Side.SELL, stop_price=None)
        decision = decide(proposal=proposal, portfolio=portfolio)
        self.assertIn("SHORT_SELL_NOT_ALLOWED", {v.code for v in decision.violations})

    def test_sell_within_existing_long_position_can_be_accepted(self):
        proposal, portfolio, _, _, _ = fixtures()
        proposal = replace(proposal, side=Side.SELL, quantity=D("2"), stop_price=None)
        portfolio = replace(portfolio, positions=(Position("us-equity-aapl", D("10"), D("1000"), "technology"),))
        decision = decide(proposal=proposal, portfolio=portfolio)
        self.assertEqual(decision.result, RiskResult.ACCEPT)

    def test_seen_proposal_and_idempotency_key_are_rejected(self):
        proposal, portfolio, *_ = fixtures()
        portfolio = replace(
            portfolio,
            processed_proposal_ids=frozenset({proposal.proposal_id}),
            processed_idempotency_keys=frozenset({proposal.idempotency_key}),
        )
        decision = decide(proposal=proposal, portfolio=portfolio)
        codes = {v.code for v in decision.violations}
        self.assertIn("DUPLICATE_PROPOSAL", codes)
        self.assertIn("DUPLICATE_IDEMPOTENCY_KEY", codes)

    def test_frequency_limit_is_enforced(self):
        _, portfolio, *_ = fixtures()
        portfolio = replace(portfolio, recent_order_times=(NOW - timedelta(seconds=1),) * 3)
        decision = decide(portfolio=portfolio)
        self.assertIn("ORDER_FREQUENCY_LIMIT", {v.code for v in decision.violations})

    def test_naive_order_history_timestamp_requires_review_without_exception(self):
        _, portfolio, *_ = fixtures()
        portfolio = replace(portfolio, recent_order_times=(NOW.replace(tzinfo=None),))
        decision = decide(portfolio=portfolio)
        self.assertEqual(decision.result, RiskResult.REVIEW_REQUIRED)
        self.assertIn("ORDER_HISTORY_TIMESTAMP_INVALID", {v.code for v in decision.violations})

    def test_wrong_runtime_collection_types_fail_closed_without_exception(self):
        _, portfolio, *_ = fixtures()
        portfolio = replace(portfolio, recent_order_times=None)
        decision = decide(portfolio=portfolio)
        self.assertEqual(decision.result, RiskResult.REVIEW_REQUIRED)
        self.assertIn("PORTFOLIO_RUNTIME_COLLECTION_INVALID", {v.code for v in decision.violations})

    def test_invalid_position_record_fails_closed_without_exception(self):
        _, portfolio, *_ = fixtures()
        portfolio = replace(portfolio, positions=(None,))
        decision = decide(portfolio=portfolio)
        self.assertEqual(decision.result, RiskResult.REJECT)
        self.assertIn("POSITION_STATE_INVALID", {v.code for v in decision.violations})

    def test_kill_switch_is_a_hard_veto(self):
        decision = decide(runtime=RiskRuntimeState(kill_switch_active=True))
        self.assertEqual(decision.result, RiskResult.REJECT)
        self.assertIn("KILL_SWITCH_ACTIVE", {v.code for v in decision.violations})

    def test_bad_policy_fails_closed_without_arithmetic_exception(self):
        _, _, _, policy, _ = fixtures()
        policy = replace(policy, max_orders_per_window="many")
        decision = decide(policy=policy)
        self.assertEqual(decision.result, RiskResult.REVIEW_REQUIRED)
        self.assertIn("POLICY_FREQUENCY_INVALID", {v.code for v in decision.violations})

    def test_empty_allowlist_never_accepts(self):
        _, _, _, policy, _ = fixtures()
        decision = decide(policy=replace(policy, allowed_instruments=frozenset()))
        self.assertEqual(decision.result, RiskResult.REVIEW_REQUIRED)
        self.assertIn("ALLOWLIST_EMPTY", {v.code for v in decision.violations})

    def test_stale_portfolio_is_rejected(self):
        _, portfolio, _, _, _ = fixtures()
        decision = decide(portfolio=replace(portfolio, as_of=NOW - timedelta(seconds=61)))
        self.assertIn("PORTFOLIO_STATE_STALE", {v.code for v in decision.violations})

    def test_zero_peak_equity_fails_closed_without_division_by_zero(self):
        _, portfolio, _, _, _ = fixtures()
        decision = decide(portfolio=replace(portfolio, peak_equity=D("0")))
        self.assertEqual(decision.result, RiskResult.REVIEW_REQUIRED)
        self.assertIn("PORTFOLIO_PEAK_EQUITY_INVALID", {v.code for v in decision.violations})

    def test_invalid_position_is_rejected(self):
        _, portfolio, *_ = fixtures()
        portfolio = replace(portfolio, positions=(Position("x", D("-1"), D("10"), "technology"),))
        decision = decide(portfolio=portfolio)
        self.assertIn("POSITION_STATE_INVALID", {v.code for v in decision.violations})

    def test_pending_orders_count_toward_exposure(self):
        proposal, portfolio, _, policy, _ = fixtures()
        portfolio = replace(portfolio, pending_exposures=(PendingExposure("other", D("19900"), "industrials"),))
        policy = replace(policy, max_gross_exposure=D("20000"))
        decision = decide(proposal=proposal, portfolio=portfolio, policy=policy)
        self.assertIn("MAX_GROSS_EXPOSURE", {v.code for v in decision.violations})

    def test_missing_required_snapshot_fails_closed(self):
        proposal, *_ = fixtures()
        decision = decide(proposal=replace(proposal, portfolio_snapshot_id=""))
        self.assertNotEqual(decision.result, RiskResult.ACCEPT)

    def test_float_policy_limit_fails_closed(self):
        _, _, _, policy, _ = fixtures()
        decision = decide(policy=replace(policy, max_order_notional=5000.0))
        self.assertEqual(decision.result, RiskResult.REVIEW_REQUIRED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
