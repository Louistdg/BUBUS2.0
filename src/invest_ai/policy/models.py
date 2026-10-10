"""Immutable Policy V0 contracts; this module does not evaluate policy."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PolicyResult(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


class PolicyMode(StrEnum):
    PAPER = "PAPER"


class PolicyReasonCode(StrEnum):
    POLICY_ACCEPTED = "POLICY_ACCEPTED"
    MODE_NOT_ALLOWED = "MODE_NOT_ALLOWED"
    RISK_NOT_ACCEPTED = "RISK_NOT_ACCEPTED"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    INVALID_PROPOSAL_HASH = "INVALID_PROPOSAL_HASH"
    INVALID_RISK_INPUT_HASH = "INVALID_RISK_INPUT_HASH"
    MISSING_MARKET_SNAPSHOT = "MISSING_MARKET_SNAPSHOT"
    MISSING_PORTFOLIO_SNAPSHOT = "MISSING_PORTFOLIO_SNAPSHOT"
    INPUT_HASH_MISMATCH = "INPUT_HASH_MISMATCH"
    POLICY_INPUT_INVALID = "POLICY_INPUT_INVALID"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Canonical, auditable outcome produced by a future Policy Engine."""

    decision_id: str
    proposal_id: str
    policy_version: str
    timestamp: datetime
    result: PolicyResult
    reason_codes: tuple[str, ...]
    mode: PolicyMode
    input_hash: str

    def __post_init__(self) -> None:
        for field_name in ("decision_id", "proposal_id", "policy_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string.")
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware.")
        if not isinstance(self.result, PolicyResult):
            raise TypeError("result must be a canonical PolicyResult.")
        if not isinstance(self.mode, PolicyMode):
            raise TypeError("mode must be a canonical PolicyMode; V0 only supports PAPER.")
        if not isinstance(self.reason_codes, tuple) or any(not isinstance(code, str) or not code.strip() for code in self.reason_codes):
            raise TypeError("reason_codes must be an immutable tuple of non-empty strings.")
        if not isinstance(self.input_hash, str) or re.fullmatch(r"[0-9a-f]{64}", self.input_hash) is None:
            raise ValueError("input_hash must be a lowercase SHA-256 hex digest.")
