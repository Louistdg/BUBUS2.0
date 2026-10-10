"""Deterministic, fail-closed Policy Engine V0 (decision only)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from invest_ai.integration import PolicyEvaluationInput, compute_policy_input_hash
from invest_ai.policy.models import (
    PolicyDecision,
    PolicyMode,
    PolicyReasonCode,
    PolicyResult,
)
from invest_ai.risk import RiskResult

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class PolicyRules:
    """Immutable operational policy settings; no financial risk limits."""

    version: str

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("PolicyRules.version must be explicit and non-empty.")


def evaluate_policy(
    policy_input: PolicyEvaluationInput,
    rules: PolicyRules,
    *,
    timestamp: datetime,
) -> PolicyDecision:
    """Evaluate one canonical Gateway input and return a decision only.

    The timestamp is mandatory and excluded from the input hash. No executor,
    broker, orchestration, or paper queue is imported or called here.
    """
    if not isinstance(policy_input, PolicyEvaluationInput):
        raise TypeError("Canonical PolicyEvaluationInput required.")
    if not isinstance(rules, PolicyRules):
        raise TypeError("Immutable PolicyRules required.")
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be explicitly timezone-aware.")
    decision_time = timestamp.astimezone(timezone.utc)

    # The Gateway hash is recomputed locally from the immutable input. There is
    # no caller-supplied input hash to trust or compare.
    input_hash = compute_policy_input_hash(policy_input)
    reasons: list[PolicyReasonCode] = []

    if policy_input.requested_mode is not PolicyMode.PAPER:
        reasons.append(PolicyReasonCode.MODE_NOT_ALLOWED)
    if policy_input.risk_result is not RiskResult.ACCEPT:
        reasons.append(PolicyReasonCode.RISK_NOT_ACCEPTED)
    if policy_input.market_data_stale is not False:
        reasons.append(PolicyReasonCode.MARKET_DATA_STALE)
    if not isinstance(policy_input.proposal_hash, str) or _SHA256.fullmatch(policy_input.proposal_hash) is None:
        reasons.append(PolicyReasonCode.INVALID_PROPOSAL_HASH)
    if not isinstance(policy_input.risk_input_hash, str) or _SHA256.fullmatch(policy_input.risk_input_hash) is None:
        reasons.append(PolicyReasonCode.INVALID_RISK_INPUT_HASH)
    if not isinstance(policy_input.risk_engine_version, str) or not policy_input.risk_engine_version.strip():
        reasons.append(PolicyReasonCode.POLICY_INPUT_INVALID)
    if not isinstance(policy_input.market_snapshot_id, str) or not policy_input.market_snapshot_id.strip():
        reasons.append(PolicyReasonCode.MISSING_MARKET_SNAPSHOT)
    if not isinstance(policy_input.portfolio_snapshot_id, str) or not policy_input.portfolio_snapshot_id.strip():
        reasons.append(PolicyReasonCode.MISSING_PORTFOLIO_SNAPSHOT)

    result = PolicyResult.REJECT if reasons else PolicyResult.ACCEPT
    if result is PolicyResult.ACCEPT:
        reasons = [PolicyReasonCode.POLICY_ACCEPTED]
    reason_codes = tuple(code.value for code in reasons)
    decision_id = _decision_id(
        proposal_id=policy_input.proposal_id,
        policy_version=rules.version,
        timestamp=decision_time,
        result=result,
        reason_codes=reason_codes,
        mode=PolicyMode.PAPER,
        input_hash=input_hash,
    )
    return PolicyDecision(
        decision_id=decision_id,
        proposal_id=policy_input.proposal_id,
        policy_version=rules.version,
        timestamp=decision_time,
        result=result,
        reason_codes=reason_codes,
        mode=PolicyMode.PAPER,
        input_hash=input_hash,
    )


def _decision_id(
    *,
    proposal_id: str,
    policy_version: str,
    timestamp: datetime,
    result: PolicyResult,
    reason_codes: tuple[str, ...],
    mode: PolicyMode,
    input_hash: str,
) -> str:
    timestamp_text = timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")
    parts = (proposal_id, policy_version, timestamp_text, result.value, ",".join(reason_codes), mode.value, input_hash)
    return "policy-" + hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
