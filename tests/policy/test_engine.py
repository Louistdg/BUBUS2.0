from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "integration"))

from test_gateway import policy_input  # noqa: E402
from invest_ai.integration import compute_policy_input_hash  # noqa: E402
from invest_ai.policy import (  # noqa: E402
    PolicyDecision,
    PolicyMode,
    PolicyReasonCode,
    PolicyResult,
    PolicyRules,
    evaluate_policy,
)
from invest_ai.policy import engine  # noqa: E402
from invest_ai.risk import RiskResult  # noqa: E402


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
RULES = PolicyRules(version="policy-v0.1")


def evaluate(value=None):
    return evaluate_policy(value or policy_input(), RULES, timestamp=NOW)


def forged_input(**changes):
    original = policy_input()
    source = object.__new__(type(original))
    for item in fields(original):
        object.__setattr__(source, item.name, changes.get(item.name, getattr(original, item.name)))
    return source


class PolicyEngineTests(unittest.TestCase):
    def test_valid_input_accepts_with_canonical_hash_and_utc_timestamp(self):
        source = policy_input()
        result = evaluate(source)
        self.assertIsInstance(result, PolicyDecision)
        self.assertIs(result.result, PolicyResult.ACCEPT)
        self.assertEqual(result.reason_codes, (PolicyReasonCode.POLICY_ACCEPTED.value,))
        self.assertIs(result.mode, PolicyMode.PAPER)
        self.assertEqual(result.input_hash, compute_policy_input_hash(source))
        self.assertEqual(result.timestamp, NOW)
        self.assertEqual(result.timestamp.utcoffset().total_seconds(), 0)

    def test_stale_market_rejects_closed(self):
        source = replace(policy_input(), market_data_stale=True)
        result = evaluate(source)
        self.assertIs(result.result, PolicyResult.REJECT)
        self.assertEqual(result.reason_codes, (PolicyReasonCode.MARKET_DATA_STALE.value,))

    def test_nonaccepted_risk_rejects_defensively(self):
        source = forged_input(risk_result=RiskResult.REJECT)
        result = evaluate(source)
        self.assertIs(result.result, PolicyResult.REJECT)
        self.assertIn(PolicyReasonCode.RISK_NOT_ACCEPTED.value, result.reason_codes)

    def test_mode_other_than_paper_cannot_be_created_through_contract(self):
        with self.assertRaises(ValueError):
            replace(policy_input(), requested_mode="LIVE")

    def test_invalid_proposal_hash_rejects_forged_frozen_input(self):
        source = forged_input(proposal_hash="invalid")
        result = evaluate(source)
        self.assertIs(result.result, PolicyResult.REJECT)
        self.assertIn(PolicyReasonCode.INVALID_PROPOSAL_HASH.value, result.reason_codes)

    def test_invalid_risk_hash_rejects_forged_frozen_input(self):
        source = forged_input(risk_input_hash="invalid")
        result = evaluate(source)
        self.assertIs(result.result, PolicyResult.REJECT)
        self.assertIn(PolicyReasonCode.INVALID_RISK_INPUT_HASH.value, result.reason_codes)

    def test_missing_snapshot_ids_reject_forged_frozen_input(self):
        source = forged_input(market_snapshot_id=" ", portfolio_snapshot_id=" ")
        result = evaluate(source)
        self.assertIs(result.result, PolicyResult.REJECT)
        self.assertIn(PolicyReasonCode.MISSING_MARKET_SNAPSHOT.value, result.reason_codes)
        self.assertIn(PolicyReasonCode.MISSING_PORTFOLIO_SNAPSHOT.value, result.reason_codes)

    def test_same_input_and_time_are_deterministic(self):
        source = policy_input()
        self.assertEqual(evaluate(source), evaluate(source))

    def test_input_hash_changes_when_proposal_or_risk_input_changes(self):
        source = policy_input()
        changed_proposal = replace(source, proposal_hash="b" * 64)
        changed_risk = replace(source, risk_input_hash="c" * 64)
        self.assertNotEqual(compute_policy_input_hash(source), compute_policy_input_hash(changed_proposal))
        self.assertNotEqual(compute_policy_input_hash(source), compute_policy_input_hash(changed_risk))

    def test_policy_models_are_immutable_and_timestamp_is_required_aware(self):
        result = evaluate()
        with self.assertRaises(FrozenInstanceError):
            result.result = PolicyResult.REJECT
        with self.assertRaises(ValueError):
            evaluate_policy(policy_input(), RULES, timestamp=datetime(2026, 10, 7))

    def test_rules_require_explicit_version(self):
        with self.assertRaises(ValueError):
            PolicyRules(version=" ")

    def test_engine_has_no_execution_or_broker_dependency(self):
        tree = ast.parse(Path(engine.__file__).read_text(encoding="utf-8"))
        imported = {
            alias.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        called = {
            node.func.id.lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        for forbidden in ("queue_paper", "claim_outbox", "broker", "executor", "backtrader", "lean"):
            self.assertFalse(any(forbidden in name for name in imported | called))


if __name__ == "__main__":
    unittest.main()
