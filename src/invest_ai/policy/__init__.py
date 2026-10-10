"""Canonical Policy contracts and deterministic V0 engine."""

from invest_ai.policy.models import PolicyDecision, PolicyMode, PolicyReasonCode, PolicyResult


def __getattr__(name: str):
    # The Integration Gateway imports PolicyMode. Lazy engine exports avoid a
    # package cycle while that Gateway itself is being initialized.
    if name in {"PolicyRules", "evaluate_policy"}:
        from invest_ai.policy.engine import PolicyRules, evaluate_policy

        return {"PolicyRules": PolicyRules, "evaluate_policy": evaluate_policy}[name]
    raise AttributeError(name)


__all__ = [
    "PolicyDecision",
    "PolicyMode",
    "PolicyReasonCode",
    "PolicyResult",
    "PolicyRules",
    "evaluate_policy",
]
