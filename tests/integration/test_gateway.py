from __future__ import annotations

import hashlib
import ast
import json
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from invest_ai.data.market import MarketSnapshot, build_market_snapshot
from invest_ai.data.models import DataProvenance, Instrument, MarketBar
from invest_ai.integration import (
    CurrencyMismatchError,
    IntegrationContractError,
    PolicyEvaluationInput,
    TrustedRiskAuthorityRequired,
    TrustedPortfolioSnapshot,
    build_policy_evaluation_input,
    build_risk_input_bundle,
    build_risk_portfolio_state,
    RiskInputBundle,
    canonical_policy_input_bytes,
    compute_policy_input_hash,
    compute_proposal_hash,
    map_market_snapshot_to_risk,
)
from invest_ai.policy import PolicyMode
from invest_ai.risk import (
    AssetType,
    PendingExposure,
    Position,
    PortfolioState,
    RiskPolicy,
    RiskDecision,
    RiskResult,
    RiskRuntimeState,
    RiskViolation,
    Side,
    TradeProposal,
    compute_input_hash,
    evaluate,
)


UTC = timezone.utc
AS_OF = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)


def make_snapshot(*, stale: bool = False) -> MarketSnapshot:
    observed = AS_OF - (timedelta(hours=3) if stale else timedelta(minutes=1))
    provenance = DataProvenance(
        provider="gateway-fixture", source="fixture://gateway", dataset="market_bar",
        instrument_id="instrument-1", observed_at=observed,
        published_at=observed + timedelta(seconds=1), received_at=observed + timedelta(seconds=2),
        available_at=observed + timedelta(seconds=2), revision=1, currency="USD", unit="USD/share",
        raw_reference="data/raw/gateway.json", raw_sha256=hashlib.sha256(b"fixture").hexdigest(),
        record_id="bar-1",
    )
    bar = MarketBar(provenance, "1d", observed - timedelta(days=1), observed,
                    Decimal("10.00"), Decimal("10.50"), Decimal("9.75"), Decimal("10.25"),
                    Decimal("100"), False)
    instrument = Instrument(
        "instrument-1", "ABC", "EQUITY", "USD", exchange="XNYS", venue="XNYS",
        timezone="America/New_York", multiplier=Decimal("100"), tick_size=Decimal("0.25"),
        lot_size=Decimal("5"), calendar_id="XNYS", price_convention="USD_PER_SHARE",
    )
    snapshot = build_market_snapshot(bar, instrument, as_of=AS_OF,
                                     max_age=timedelta(minutes=30))
    if stale and not snapshot.stale:
        raise AssertionError("fixture expected stale snapshot")
    return snapshot


def portfolio_input(*, currency: str = "USD") -> TrustedPortfolioSnapshot:
    return TrustedPortfolioSnapshot(
        snapshot_id="portfolio-1", as_of=AS_OF - timedelta(seconds=5),
        cash=Decimal("10000"), equity=Decimal("10000"), peak_equity=Decimal("10500"),
        daily_pnl=Decimal("0"), base_currency=currency,
        positions=(Position("instrument-1", Decimal("5"), Decimal("5125")),),
        pending_exposures=(PendingExposure("other", Decimal("12.50")),),
        recent_order_times=(AS_OF - timedelta(minutes=2),),
        processed_proposal_ids=frozenset(), processed_idempotency_keys=frozenset(),
    )


def proposal(snapshot: MarketSnapshot, portfolio_id: str = "portfolio-1") -> TradeProposal:
    return TradeProposal(
        proposal_id="proposal-1", idempotency_key="idem-1", instrument_id="instrument-1", symbol="ABC",
        asset_type=AssetType.EQUITY, side=Side.BUY, quantity=Decimal("5"), strategy_id="strategy-1",
        created_at=AS_OF - timedelta(seconds=2), stop_price=Decimal("9.00"),
        market_snapshot_id=snapshot.snapshot_id, portfolio_snapshot_id=portfolio_id,
    )


def risk_policy(*, max_market_data_age_seconds: int = 14400) -> RiskPolicy:
    return RiskPolicy(
        version="risk-policy-v1", allowed_instruments=frozenset({"instrument-1"}),
        allowed_sides=frozenset({Side.BUY, Side.SELL}),
        allowed_asset_types=frozenset({AssetType.EQUITY, AssetType.ETF}),
        max_order_notional=Decimal("100000"), max_position_notional=Decimal("200000"),
        max_concentration_ratio=Decimal("1"), max_sector_concentration_ratio=Decimal("1"),
        max_gross_exposure=Decimal("300000"), min_cash_balance=Decimal("1"),
        max_loss_per_trade=Decimal("10000"), max_daily_loss=Decimal("20000"),
        max_drawdown_ratio=Decimal("0.9"), max_leverage=Decimal("20"),
        max_market_data_age_seconds=max_market_data_age_seconds, max_portfolio_age_seconds=3600,
        max_orders_per_window=100, frequency_window_seconds=3600,
        fee_rate=Decimal("0.001"), slippage_buffer_rate=Decimal("0.001"),
    )


def risk_bundle(snapshot: MarketSnapshot | None = None,
                trusted: TrustedPortfolioSnapshot | None = None,
                *, evaluated_at: datetime = AS_OF) -> RiskInputBundle:
    if trusted is None:
        trusted = replace(portfolio_input(), equity=Decimal("20000"), peak_equity=Decimal("20000"))
    return build_risk_input_bundle(snapshot or make_snapshot(), trusted,
                                   evaluation_time=evaluated_at, market_open=True, sector="industrials")


def accepted_risk(p: TradeProposal, *, snapshot: MarketSnapshot | None = None,
                  trusted: TrustedPortfolioSnapshot | None = None, bundle: RiskInputBundle | None = None,
                  policy: RiskPolicy | None = None, runtime: RiskRuntimeState | None = None,
                  evaluated_at: datetime = AS_OF) -> RiskDecision:
    snapshot = snapshot or make_snapshot()
    bundle = bundle or risk_bundle(snapshot, trusted, evaluated_at=evaluated_at)
    policy = policy or risk_policy()
    runtime = runtime or RiskRuntimeState()
    return evaluate(p, bundle.portfolio, bundle.market.market_state, policy, runtime, evaluated_at)


def policy_input(snapshot: MarketSnapshot | None = None, p: TradeProposal | None = None,
                 risk: RiskDecision | None = None, portfolio: PortfolioState | None = None,
                 policy: RiskPolicy | None = None, runtime: RiskRuntimeState | None = None,
                 evaluated_at: datetime = AS_OF) -> PolicyEvaluationInput:
    snapshot = snapshot or make_snapshot()
    p = p or proposal(snapshot)
    policy = policy or risk_policy()
    runtime = runtime or RiskRuntimeState()
    if portfolio is not None:
        trusted = portfolio_input()
        bundle = RiskInputBundle(map_market_snapshot_to_risk(
            snapshot, evaluation_time=evaluated_at, market_open=True, sector="industrials"), portfolio)
    else:
        bundle = risk_bundle(snapshot, evaluated_at=evaluated_at)
        trusted = replace(portfolio_input(), equity=Decimal("20000"), peak_equity=Decimal("20000"))
    risk = risk or accepted_risk(p, snapshot=snapshot, trusted=trusted, bundle=bundle,
                                 policy=policy, runtime=runtime, evaluated_at=evaluated_at)
    # Synthetic envelope for pure Policy contract tests only. This deliberately
    # bypasses no Gateway: callers cannot promote it through Orchestration Core.
    return PolicyEvaluationInput(
        proposal_id=p.proposal_id,
        proposal_hash=compute_proposal_hash(p),
        risk_decision_id=risk.decision_id,
        risk_input_hash=risk.input_hash,
        risk_result=risk.result,
        risk_engine_version=risk.engine_version,
        market_snapshot_id=snapshot.snapshot_id,
        market_data_stale=snapshot.stale,
        portfolio_snapshot_id=bundle.portfolio.snapshot_id,
        requested_mode=PolicyMode.PAPER,
    )


class IntegrationGatewayTests(unittest.TestCase):
    def test_market_snapshot_maps_all_risk_fields_without_inventing_facts(self):
        snapshot = make_snapshot()
        mapped = map_market_snapshot_to_risk(snapshot, evaluation_time=AS_OF)
        state = mapped.market_state
        self.assertEqual(state.snapshot_id, snapshot.snapshot_id)
        self.assertEqual(state.instrument_id, snapshot.instrument_id)
        self.assertEqual(state.observed_at, snapshot.observed_at)
        self.assertEqual(state.price, snapshot.price)
        self.assertEqual(state.currency, snapshot.currency)
        self.assertEqual(state.venue, snapshot.venue)
        self.assertEqual(state.multiplier, snapshot.multiplier)
        self.assertEqual(state.tick_size, snapshot.tick_size)
        self.assertEqual(state.lot_size, snapshot.lot_size)
        self.assertIsNone(state.market_open)
        self.assertIsNone(state.sector)
        self.assertEqual(mapped.data_as_of, snapshot.as_of)
        self.assertEqual(mapped.published_at, snapshot.published_at)
        self.assertEqual(mapped.received_at, snapshot.received_at)
        self.assertEqual(mapped.available_at, snapshot.available_at)

    def test_trusted_market_open_and_sector_are_passed_through(self):
        snapshot = make_snapshot()
        mapped = map_market_snapshot_to_risk(snapshot, evaluation_time=AS_OF, market_open=True, sector="industrials")
        self.assertIs(mapped.market_state.market_open, True)
        self.assertEqual(mapped.market_state.sector, "industrials")

    def test_stale_flag_is_preserved_without_refreshing_observed_at(self):
        snapshot = make_snapshot(stale=True)
        mapped = map_market_snapshot_to_risk(snapshot, evaluation_time=AS_OF)
        self.assertTrue(mapped.data_stale)
        self.assertEqual(mapped.market_state.observed_at, snapshot.observed_at)
        self.assertLess(mapped.market_state.observed_at, AS_OF - timedelta(hours=2))

    def test_future_snapshot_is_refused(self):
        snapshot = make_snapshot()
        with self.assertRaisesRegex(IntegrationContractError, "future market data"):
            map_market_snapshot_to_risk(snapshot, evaluation_time=AS_OF - timedelta(seconds=1))

    def test_invalid_instrument_units_fail_closed_without_rounding(self):
        snapshot = replace(make_snapshot(), multiplier=Decimal("0"))
        with self.assertRaisesRegex(IntegrationContractError, "multiplier must be positive"):
            map_market_snapshot_to_risk(snapshot, evaluation_time=AS_OF)
        snapshot = replace(make_snapshot(), tick_size=Decimal("0"))
        with self.assertRaisesRegex(IntegrationContractError, "tick_size must be positive"):
            map_market_snapshot_to_risk(snapshot, evaluation_time=AS_OF)
        snapshot = replace(make_snapshot(), lot_size=Decimal("0"))
        with self.assertRaisesRegex(IntegrationContractError, "lot_size must be positive"):
            map_market_snapshot_to_risk(snapshot, evaluation_time=AS_OF)

    def test_portfolio_boundary_requires_base_currency_and_decimals(self):
        trusted = portfolio_input()
        risk_portfolio = build_risk_portfolio_state(trusted)
        self.assertEqual(risk_portfolio.base_currency, "USD")
        self.assertEqual(risk_portfolio.positions[0].market_value, Decimal("5125"))
        self.assertEqual(risk_portfolio.pending_exposures[0].notional, Decimal("12.50"))
        with self.assertRaises(TypeError):
            PortfolioState("id", AS_OF, Decimal(1), Decimal(1), Decimal(1), Decimal(0))
        with self.assertRaises(TypeError):
            TrustedPortfolioSnapshot("id", AS_OF, Decimal(1), Decimal(1), Decimal(1), Decimal(0),
                                     positions=(), pending_exposures=(), recent_order_times=(),
                                     processed_proposal_ids=frozenset(), processed_idempotency_keys=frozenset())
        with self.assertRaisesRegex(IntegrationContractError, "finite Decimal"):
            build_risk_portfolio_state(replace(trusted, cash=1.0))

    def test_market_currency_must_equal_portfolio_base_currency(self):
        snapshot = make_snapshot()
        p = proposal(snapshot)
        policy = risk_policy()
        runtime = RiskRuntimeState()
        bundle = risk_bundle(snapshot)
        risk = accepted_risk(p, snapshot=snapshot, bundle=bundle, policy=policy, runtime=runtime)
        trusted_portfolio = portfolio_input(currency="EUR")
        with self.assertRaises(CurrencyMismatchError):
            build_risk_input_bundle(snapshot, trusted_portfolio, evaluation_time=AS_OF,
                                    market_open=True, sector="industrials")
        portfolio = build_risk_portfolio_state(trusted_portfolio)
        mapped = map_market_snapshot_to_risk(snapshot, evaluation_time=AS_OF,
                                             market_open=True, sector="industrials")
        with self.assertRaises(CurrencyMismatchError):
            build_policy_evaluation_input(p, risk, snapshot, RiskInputBundle(mapped, portfolio),
                                          policy, runtime, AS_OF)

    def test_proposal_hash_is_deterministic_and_changes_with_proposal(self):
        snapshot = make_snapshot()
        p = proposal(snapshot)
        self.assertEqual(compute_proposal_hash(p), compute_proposal_hash(p))
        self.assertNotEqual(compute_proposal_hash(p), compute_proposal_hash(replace(p, quantity=Decimal("6"))))
        self.assertEqual(compute_proposal_hash(replace(p, quantity=Decimal("5.0"))), compute_proposal_hash(p))

    def test_policy_hash_is_deterministic_and_excludes_wall_clock(self):
        value = policy_input()
        self.assertEqual(compute_policy_input_hash(value), compute_policy_input_hash(value))
        self.assertEqual(len(compute_policy_input_hash(value)), 64)
        self.assertEqual(compute_policy_input_hash(value), compute_policy_input_hash(replace(value)))
        self.assertEqual(compute_policy_input_hash(value), hashlib.sha256(canonical_policy_input_bytes(value)).hexdigest())

    def test_policy_hash_changes_for_proposal_risk_market_and_portfolio_inputs(self):
        value = policy_input()
        digest = compute_policy_input_hash(value)
        changed = (
            replace(value, proposal_hash="b" * 64),
            replace(value, risk_decision_id="risk-changed"),
            replace(value, risk_input_hash="b" * 64),
            replace(value, risk_engine_version="3"),
            replace(value, market_snapshot_id="market-changed"),
            replace(value, market_data_stale=not value.market_data_stale),
            replace(value, portfolio_snapshot_id="portfolio-changed"),
        )
        for item in changed:
            with self.subTest(item=item):
                self.assertNotEqual(compute_policy_input_hash(item), digest)
        canonical = json.loads(canonical_policy_input_bytes(value))
        self.assertEqual(canonical["requested_mode"], "PAPER")
        mode_mutation_bytes = canonical_policy_input_bytes(value).replace(
            b'"requested_mode":"PAPER"', b'"requested_mode":"LIVE"',
        )
        self.assertNotEqual(hashlib.sha256(mode_mutation_bytes).hexdigest(), digest)

    def test_policy_input_requires_paper_only(self):
        with self.assertRaises(ValueError):
            PolicyMode("LIVE")
        with self.assertRaises(IntegrationContractError):
            replace(policy_input(), requested_mode="LIVE")

    def test_caller_supplied_risk_never_authorizes_policy_input(self):
        snapshot = make_snapshot()
        p = proposal(snapshot)
        policy = risk_policy()
        runtime = RiskRuntimeState()
        bundle = risk_bundle(snapshot)
        risk = accepted_risk(p, snapshot=snapshot, bundle=bundle, policy=policy, runtime=runtime)
        with self.assertRaises(TrustedRiskAuthorityRequired):
            build_policy_evaluation_input(p, risk, snapshot, bundle, policy, runtime, AS_OF)
        for result_code in (RiskResult.REJECT, RiskResult.REVIEW_REQUIRED):
            with self.subTest(result=result_code):
                with self.assertRaisesRegex(IntegrationContractError, "not eligible"):
                    build_policy_evaluation_input(p, replace(risk, result=result_code), snapshot,
                                                  bundle, policy, runtime, AS_OF)
        with self.assertRaisesRegex(IntegrationContractError, "does not match"):
            build_policy_evaluation_input(p, replace(risk, proposal_id="another"), snapshot,
                                          bundle, policy, runtime, AS_OF)

    def test_forged_accept_with_exact_hash_cannot_bypass_active_kill_switch(self):
        snapshot = make_snapshot()
        p = proposal(snapshot)
        bundle = risk_bundle(snapshot)
        policy = risk_policy()
        runtime = RiskRuntimeState(kill_switch_active=True)
        rejected = evaluate(p, bundle.portfolio, bundle.market.market_state, policy, runtime, AS_OF)
        self.assertIs(rejected.result, RiskResult.REJECT)
        forged = replace(rejected, result=RiskResult.ACCEPT, violations=())
        self.assertEqual(
            forged.input_hash,
            compute_input_hash(p, bundle.portfolio, bundle.market.market_state, policy, runtime, AS_OF),
        )
        with self.assertRaises(TrustedRiskAuthorityRequired):
            build_policy_evaluation_input(p, forged, snapshot, bundle, policy, runtime, AS_OF)

    def test_exact_replay_binds_all_six_risk_inputs_and_is_deterministic(self):
        snapshot = make_snapshot()
        p = proposal(snapshot)
        bundle = risk_bundle(snapshot)
        policy = risk_policy()
        runtime = RiskRuntimeState()
        risk = accepted_risk(p, snapshot=snapshot, bundle=bundle, policy=policy,
                             runtime=runtime, evaluated_at=AS_OF)
        self.assertEqual(compute_input_hash(p, bundle.portfolio, bundle.market.market_state,
                                            policy, runtime, AS_OF), risk.input_hash)
        with self.assertRaises(TrustedRiskAuthorityRequired):
            build_policy_evaluation_input(p, risk, snapshot, bundle, policy, runtime, AS_OF)

    def test_any_proposal_change_invalidates_previous_risk_accept(self):
        snapshot = make_snapshot()
        p = proposal(snapshot)
        bundle = risk_bundle(snapshot)
        policy = risk_policy()
        runtime = RiskRuntimeState()
        risk = accepted_risk(p, snapshot=snapshot, bundle=bundle, policy=policy, runtime=runtime)
        mutations = (
            replace(p, quantity=Decimal("6")),
            replace(p, side=Side.SELL),
            replace(p, limit_price=Decimal("10.00")),
            replace(p, stop_price=Decimal("8.50")),
            replace(p, order_type="LIMIT"),
            replace(p, instrument_id="instrument-2"),
            replace(p, strategy_id="strategy-changed"),
            replace(p, evidence_refs=("different-evidence",)),
            replace(p, market_snapshot_id="market-replaced"),
            replace(p, portfolio_snapshot_id="portfolio-replaced"),
        )
        for changed in mutations:
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(IntegrationContractError, "input_hash|does not match"):
                    build_policy_evaluation_input(changed, risk, snapshot, bundle, policy, runtime, AS_OF)

    def test_runtime_policy_and_evaluated_at_are_bound(self):
        snapshot = make_snapshot()
        p = proposal(snapshot)
        bundle = risk_bundle(snapshot)
        policy = risk_policy()
        runtime = RiskRuntimeState()
        risk = accepted_risk(p, snapshot=snapshot, bundle=bundle, policy=policy, runtime=runtime)
        with self.assertRaisesRegex(IntegrationContractError, "input_hash"):
            build_policy_evaluation_input(p, risk, snapshot, bundle,
                                          replace(policy, max_market_data_age_seconds=1000), runtime, AS_OF)
        with self.assertRaisesRegex(IntegrationContractError, "input_hash"):
            build_policy_evaluation_input(p, risk, snapshot, bundle, policy,
                                          RiskRuntimeState(kill_switch_active=True), AS_OF)
        with self.assertRaisesRegex(IntegrationContractError, "timestamp"):
            build_policy_evaluation_input(p, risk, snapshot, bundle, policy, runtime,
                                          AS_OF + timedelta(seconds=1))
        with self.assertRaisesRegex(IntegrationContractError, "input_hash"):
            build_policy_evaluation_input(p, replace(risk, input_hash="f" * 64), snapshot,
                                          bundle, policy, runtime, AS_OF)

    def test_market_snapshot_replacements_cannot_reuse_risk_accept(self):
        original = make_snapshot()
        p = proposal(original)
        bundle = risk_bundle(original)
        policy = risk_policy()
        runtime = RiskRuntimeState()
        risk = accepted_risk(p, snapshot=original, bundle=bundle, policy=policy, runtime=runtime)
        changed_snapshots = (
            replace(original, observed_at=original.observed_at - timedelta(minutes=1)),
            replace(original, price=Decimal("11.00")),
            replace(original, multiplier=Decimal("50")),
            replace(original, raw_sha256="b" * 64),
        )
        for changed_snapshot in changed_snapshots:
            changed_bundle = risk_bundle(changed_snapshot)
            changed_proposal = replace(p, market_snapshot_id=changed_snapshot.snapshot_id)
            with self.subTest(snapshot=changed_snapshot.snapshot_id):
                with self.assertRaises(IntegrationContractError):
                    build_policy_evaluation_input(changed_proposal, risk, changed_snapshot,
                                                  changed_bundle, policy, runtime, AS_OF)
        # The original DATA snapshot also cannot be paired with a replaced old Risk bundle.
        replaced_snapshot = replace(original, price=Decimal("10.75"))
        with self.assertRaisesRegex(IntegrationContractError, "MarketSnapshot"):
            build_policy_evaluation_input(p, risk, replaced_snapshot, bundle, policy, runtime, AS_OF)

    def test_currency_and_portfolio_snapshot_replacements_cannot_reuse_risk_accept(self):
        snapshot = make_snapshot()
        p = proposal(snapshot)
        bundle = risk_bundle(snapshot)
        policy = risk_policy()
        runtime = RiskRuntimeState()
        risk = accepted_risk(p, snapshot=snapshot, bundle=bundle, policy=policy, runtime=runtime)
        altered_portfolio_source = replace(portfolio_input(), snapshot_id="portfolio-B", cash=Decimal("9000"),
                                           equity=Decimal("20000"), peak_equity=Decimal("20000"))
        altered_portfolio_bundle = risk_bundle(snapshot, altered_portfolio_source)
        changed_proposal = replace(p, portfolio_snapshot_id="portfolio-B")
        with self.assertRaisesRegex(IntegrationContractError, "input_hash"):
            build_policy_evaluation_input(changed_proposal, risk, snapshot, altered_portfolio_bundle,
                                          policy, runtime, AS_OF)

        euro_snapshot = replace(snapshot, currency="EUR")
        euro_portfolio = replace(portfolio_input(currency="EUR"), equity=Decimal("20000"),
                                 peak_equity=Decimal("20000"))
        euro_bundle = risk_bundle(euro_snapshot, euro_portfolio)
        euro_proposal = replace(p, market_snapshot_id=euro_snapshot.snapshot_id)
        with self.assertRaisesRegex(IntegrationContractError, "input_hash"):
            build_policy_evaluation_input(euro_proposal, risk, euro_snapshot, euro_bundle,
                                          policy, runtime, AS_OF)

    def test_untyped_decimal_marker_collision_is_rejected(self):
        snapshot = make_snapshot()
        malformed = replace(proposal(snapshot), quantity={"$decimal": "5"})
        with self.assertRaisesRegex(IntegrationContractError, "finite Decimal"):
            compute_proposal_hash(malformed)

    def test_three_hour_snapshot_marked_fresh_is_still_rejected_by_risk(self):
        old_snapshot = replace(make_snapshot(), observed_at=AS_OF - timedelta(hours=3), stale=False)
        p = proposal(old_snapshot)
        bundle = risk_bundle(old_snapshot)
        strict_policy = risk_policy(max_market_data_age_seconds=60)
        runtime = RiskRuntimeState()
        decision = evaluate(p, bundle.portfolio, bundle.market.market_state,
                            strict_policy, runtime, AS_OF)
        self.assertIs(decision.result, RiskResult.REJECT)
        self.assertIn("MARKET_DATA_STALE", {item.code for item in decision.violations})
        self.assertFalse(old_snapshot.stale)
        with self.assertRaisesRegex(IntegrationContractError, "not eligible"):
            build_policy_evaluation_input(p, decision, old_snapshot, bundle,
                                          strict_policy, runtime, AS_OF)

    def test_gateway_has_no_paper_or_broker_execution_dependency(self):
        source = ast.parse(Path(__file__).resolve().parents[2].joinpath(
            "src", "invest_ai", "integration", "gateway.py").read_text(encoding="utf-8"))
        imported = {alias.name.lower() for node in ast.walk(source)
                    if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
        called = {node.func.id.lower() for node in ast.walk(source)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        for forbidden in ("queue_paper", "claim_outbox", "broker", "executor", "backtrader", "lean"):
            self.assertFalse(any(forbidden in name for name in imported | called))

    def test_policy_input_preserves_data_stale_marker_from_snapshot(self):
        snapshot = make_snapshot(stale=True)
        p = proposal(snapshot)
        policy = risk_policy()
        runtime = RiskRuntimeState()
        bundle = risk_bundle(snapshot)
        risk = accepted_risk(p, snapshot=snapshot, bundle=bundle, policy=policy, runtime=runtime)
        with self.assertRaises(TrustedRiskAuthorityRequired):
            build_policy_evaluation_input(p, risk, snapshot, bundle, policy, runtime, AS_OF)

    def test_gateway_only_builds_inputs_and_does_not_queue_or_execute(self):
        envelope = policy_input()
        self.assertFalse(hasattr(envelope, "outbox_id"))
        self.assertFalse(hasattr(envelope, "queue_paper"))
        self.assertFalse(hasattr(envelope, "execute"))


if __name__ == "__main__":
    unittest.main()
