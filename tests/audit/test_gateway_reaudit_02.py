"""Independent adversarial regression suite for Gateway correction 01B."""
from __future__ import annotations

import ast
import hashlib
import inspect
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "integration"))

import test_gateway as fx
from invest_ai.data.market import MarketSnapshot
from invest_ai.integration import (
    IntegrationContractError, RiskInputBundle, TrustedRiskAuthorityRequired, build_policy_evaluation_input,
    build_risk_input_bundle, map_market_snapshot_to_risk,
)
from invest_ai.risk import (
    PendingExposure, Position, RiskDecision, RiskPolicy, RiskResult,
    RiskRuntimeState, Side, TradeProposal, compute_input_hash, evaluate,
)


class GatewayReaudit02(unittest.TestCase):
    def setUp(self):
        self.snapshot = fx.make_snapshot()
        self.proposal = fx.proposal(self.snapshot)
        self.bundle = fx.risk_bundle(self.snapshot)
        self.policy = fx.risk_policy()
        self.runtime = RiskRuntimeState()
        self.risk = fx.accepted_risk(
            self.proposal, snapshot=self.snapshot, bundle=self.bundle,
            policy=self.policy, runtime=self.runtime, evaluated_at=fx.AS_OF,
        )

    def build(self, p=None, snapshot=None, bundle=None, policy=None, runtime=None,
              risk=None, at=None):
        return build_policy_evaluation_input(
            p or self.proposal, risk or self.risk, snapshot or self.snapshot,
            bundle or self.bundle, policy or self.policy, runtime or self.runtime,
            at or fx.AS_OF,
        )

    def assert_rejected(self, **kwargs):
        with self.assertRaises((IntegrationContractError, TypeError, ValueError)):
            self.build(**kwargs)

    def test_exact_six_input_replay_and_utc_equivalent_instant(self):
        offset_time = fx.AS_OF.astimezone(fx.timezone(timedelta(hours=2)))
        self.assertEqual(compute_input_hash(
            self.proposal, self.bundle.portfolio, self.bundle.market.market_state,
            self.policy, self.runtime, offset_time,
        ), self.risk.input_hash)
        # A valid exact-input hash still cannot replace a Core-issued Risk receipt.
        with self.assertRaises(TrustedRiskAuthorityRequired):
            self.build()
        with self.assertRaises(TrustedRiskAuthorityRequired):
            self.build(at=offset_time)

    def test_all_proposal_field_substitutions_rejected(self):
        changes = (
            {"quantity": Decimal("6")}, {"side": Side.SELL},
            {"order_type": "LIMIT"}, {"limit_price": Decimal("10.25")},
            {"stop_price": Decimal("8.75")}, {"instrument_id": "instrument-2"},
            {"strategy_id": "strategy-2"}, {"evidence_refs": ("evidence://other",)},
            {"market_snapshot_id": "market-2"}, {"portfolio_snapshot_id": "portfolio-2"},
            {"symbol": "XYZ"}, {"idempotency_key": "idem-2"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assert_rejected(p=replace(self.proposal, **change))

    def test_all_market_state_substitutions_rejected(self):
        changes = (
            {"price": Decimal("10.75")},
            {"observed_at": self.bundle.market.market_state.observed_at - timedelta(seconds=1)},
            {"currency": "EUR"}, {"multiplier": Decimal("50")},
            {"tick_size": Decimal("0.50")}, {"lot_size": Decimal("10")},
            {"market_open": False}, {"sector": "technology"},
            {"venue": "XNAS"}, {"instrument_id": "instrument-2"},
            {"snapshot_id": "market-2"},
        )
        for change in changes:
            changed_state = replace(self.bundle.market.market_state, **change)
            changed_bundle = replace(self.bundle, market=replace(self.bundle.market, market_state=changed_state))
            with self.subTest(change=change):
                self.assert_rejected(bundle=changed_bundle)

    def test_data_snapshot_replacement_rejected(self):
        changes = (
            {"price": Decimal("10.75")},
            {"observed_at": self.snapshot.observed_at - timedelta(seconds=1)},
            {"currency": "EUR"}, {"multiplier": Decimal("50")},
            {"tick_size": Decimal("0.50")}, {"lot_size": Decimal("10")},
            {"raw_sha256": hashlib.sha256(b"changed raw").hexdigest()},
            {"instrument_id": "instrument-2"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assert_rejected(snapshot=replace(self.snapshot, **change))

    def test_all_portfolio_substitutions_rejected(self):
        p0 = self.bundle.portfolio
        changes = (
            {"snapshot_id": "portfolio-2"}, {"cash": Decimal("9000")},
            {"equity": Decimal("19000")}, {"peak_equity": Decimal("21000")},
            {"daily_pnl": Decimal("-10")},
            {"positions": (Position("instrument-1", Decimal("6"), Decimal("6150")),)},
            {"pending_exposures": (PendingExposure("other", Decimal("99")),)},
            {"as_of": p0.as_of - timedelta(seconds=1)},
            {"processed_proposal_ids": frozenset({"proposal-old"})},
            {"processed_idempotency_keys": frozenset({"idem-old"})},
        )
        for change in changes:
            changed_bundle = replace(self.bundle, portfolio=replace(p0, **change))
            with self.subTest(change=change):
                self.assert_rejected(bundle=changed_bundle)

    def test_policy_runtime_time_and_decision_tampering_rejected(self):
        self.assert_rejected(policy=replace(self.policy, max_order_notional=Decimal("99999")))
        self.assert_rejected(policy=replace(self.policy, max_market_data_age_seconds=1000))
        self.assert_rejected(runtime=RiskRuntimeState(kill_switch_active=True))
        self.assert_rejected(at=fx.AS_OF + timedelta(seconds=1))
        self.assert_rejected(risk=replace(self.risk, input_hash="f" * 64))
        self.assert_rejected(risk=replace(self.risk, proposal_id="proposal-other"))
        self.assert_rejected(risk=replace(self.risk, engine_version="999"))
        self.assert_rejected(risk=replace(self.risk, policy_version="policy-other"))

    def test_forged_accept_with_correct_hash_bypasses_active_kill_switch(self):
        """Attack reproduction: a content hash is not proof Risk issued the result."""
        active_runtime = RiskRuntimeState(kill_switch_active=True)
        actual = evaluate(self.proposal, self.bundle.portfolio, self.bundle.market.market_state,
                          self.policy, active_runtime, fx.AS_OF)
        self.assertIs(actual.result, RiskResult.REJECT)
        digest = compute_input_hash(
            self.proposal, self.bundle.portfolio, self.bundle.market.market_state,
            self.policy, active_runtime, fx.AS_OF,
        )
        forged = replace(actual, result=RiskResult.ACCEPT, input_hash=digest)
        # This demonstrates the boundary: matching all six values is insufficient
        # to establish that the trusted Risk engine generated this decision.
        with self.assertRaises(TrustedRiskAuthorityRequired):
            self.build(risk=forged, runtime=active_runtime)

    def test_reject_and_review_required_never_yield_policy_input(self):
        for result in (RiskResult.REJECT, RiskResult.REVIEW_REQUIRED):
            with self.subTest(result=result):
                self.assert_rejected(risk=replace(self.risk, result=result))

    def test_old_market_marked_fresh_is_refused_by_risk_before_gateway(self):
        old_snapshot = replace(
            self.snapshot,
            observed_at=fx.AS_OF - timedelta(hours=3),
            as_of=fx.AS_OF, stale=False,
        )
        old_bundle = build_risk_input_bundle(
            old_snapshot, fx.portfolio_input(), evaluation_time=fx.AS_OF,
            market_open=True, sector="industrials",
        )
        strict = fx.risk_policy(max_market_data_age_seconds=60)
        old_proposal = fx.proposal(old_snapshot)
        decision = evaluate(old_proposal, old_bundle.portfolio, old_bundle.market.market_state,
                            strict, self.runtime, fx.AS_OF)
        self.assertIs(decision.result, RiskResult.REJECT)
        self.assertIn("MARKET_DATA_STALE", {v.code for v in decision.violations})
        with self.assertRaises(IntegrationContractError):
            build_policy_evaluation_input(old_proposal, decision, old_snapshot, old_bundle,
                                          strict, self.runtime, fx.AS_OF)

    def test_malformed_decimal_marker_and_wrong_typed_objects_are_rejected(self):
        malformed = replace(self.proposal, quantity={"$decimal": "5"})
        self.assert_rejected(p=malformed)
        malformed_bundle = replace(self.bundle, portfolio=replace(self.bundle.portfolio, cash=5.0))
        self.assert_rejected(bundle=malformed_bundle)

    def test_gateway_uses_risk_public_hash_as_only_risk_hash_authority(self):
        from invest_ai.integration import gateway
        source = inspect.getsource(gateway)
        self.assertIn("from invest_ai.risk import ENGINE_VERSION, compute_input_hash", source)
        self.assertEqual(source.count("compute_input_hash("), 1)
        self.assertNotIn("_hash_inputs", source)
        builder_source = inspect.getsource(gateway.build_policy_evaluation_input)
        self.assertNotIn("_canonical(", builder_source)

    def test_no_execution_dependencies_or_network_calls(self):
        path = ROOT / "src" / "invest_ai" / "integration" / "gateway.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {alias.name.lower() for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
        forbidden = ("broker", "backtrader", "lean", "urllib", "requests", "httpx", "socket")
        self.assertFalse([name for name in imported if any(x in name for x in forbidden)])
        source = path.read_text(encoding="utf-8").lower()
        self.assertNotIn("place_order", source)
        self.assertNotIn("claim_outbox", source)


if __name__ == "__main__":
    unittest.main()
