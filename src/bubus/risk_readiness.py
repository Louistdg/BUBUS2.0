"""BUBUS2.0 read-only Risk readiness audit; no authorization or order capability.

The inherited deterministic Risk engine is not connected to trading. This
module never constructs an active RiskPolicy, approval receipt, or order gate.
It audits explicit local draft limits against available evidence and refuses
to substitute observational portfolio valuations for reconciled P&L.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_NUMBER = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,8})?\Z")
_DRAFT_KEYS = {"candidate_id", "created_at", "base_currency",
               "capital_limit", "max_order_notional", "max_daily_loss",
               "payload_sha256", "approval_status", "active", "orders_enabled"}


class RiskReadinessError(ValueError):
    """A local Risk input is structurally invalid or contradicts its status."""


def _aware(stamp: object) -> None:
    if not isinstance(stamp, str):
        raise RiskReadinessError("Untrusted timestamp type.")
    try:
        instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        raise RiskReadinessError("Untrusted timestamp value.") from None
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise RiskReadinessError("Timestamp must include a timezone.")


def _money(value: object, field: str) -> Decimal:
    if not isinstance(value, str) or _NUMBER.fullmatch(value) is None:
        raise RiskReadinessError(f"{field} is not an explicitly configured decimal.")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise RiskReadinessError(f"{field} is not valid.") from None
    if not amount.is_finite() or amount <= 0:
        raise RiskReadinessError(f"{field} must be finite and positive.")
    return amount


def validate_draft(draft: object) -> dict | None:
    """Check the exact draft contract. Never promote a candidate to active."""
    if draft is None:
        return None
    if not isinstance(draft, dict) or set(draft) != _DRAFT_KEYS:
        raise RiskReadinessError("Unexpected Risk limit draft shape.")
    if (type(draft["candidate_id"]) is not int or draft["candidate_id"] <= 0
            or not isinstance(draft["base_currency"], str)
            or _CURRENCY.fullmatch(draft["base_currency"]) is None
            or not isinstance(draft["payload_sha256"], str)
            or _SHA256.fullmatch(draft["payload_sha256"]) is None
            or draft["approval_status"] != "PENDING_OWNER_REVIEW"
            or draft["active"] is not False or draft["orders_enabled"] is not False):
        raise RiskReadinessError("Risk draft provenance or authority is invalid.")
    _aware(draft["created_at"])
    cap = _money(draft["capital_limit"], "capital_limit")
    order = _money(draft["max_order_notional"], "max_order_notional")
    daily = _money(draft["max_daily_loss"], "max_daily_loss")
    if order > cap or daily > cap:
        raise RiskReadinessError("Draft limits exceed the explicitly specified capital.")
    material = {key: draft[key] for key in
                ("base_currency", "capital_limit", "max_order_notional", "max_daily_loss")}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != draft["payload_sha256"]:
        raise RiskReadinessError("Risk draft hash does not match its financial limits.")
    return {"candidate_id": draft["candidate_id"], "base_currency": draft["base_currency"],
            "capital_limit": draft["capital_limit"],
            "max_order_notional": draft["max_order_notional"],
            "max_daily_loss": draft["max_daily_loss"],
            "created_at": draft["created_at"], "payload_sha256": draft["payload_sha256"],
            "approval_status": "PENDING_OWNER_REVIEW", "active": False}


def assess_draft_order_cap(*, draft: dict | None, order_notional: object,
                           currency: object) -> dict:
    """Simulated *single* order amount check; never an execution decision.

    The cash state, daily P&L, working orders, fees, FX, user approval and
    authenticated policy are still unknown. Therefore PASS_LIMITS_ONLY is not
    a tradable / Risk ACCEPT result, even for a small proposed order.
    """
    checked = validate_draft(draft)
    if checked is None:
        return {"status": "BLOCKED", "reasons": ["OWNER_LIMITS_MISSING"],
                "orders_enabled": False, "usable_for_execution": False}
    if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
        raise RiskReadinessError("Simulated notional currency is invalid.")
    notional = _money(order_notional, "order_notional")
    reasons = []
    if currency != checked["base_currency"]:
        reasons.append("FX_UNVERIFIED")
    else:
        if notional > Decimal(checked["max_order_notional"]):
            reasons.append("MAX_ORDER_NOTIONAL_EXCEEDED")
        if notional > Decimal(checked["capital_limit"]):
            reasons.append("CAPITAL_LIMIT_EXCEEDED")
    return {"status": "BLOCKED", "limit_comparison": "REJECT_LIMITS" if reasons else "PASS_LIMITS_ONLY",
            "reasons": list(dict.fromkeys(reasons + [
                "POLICY_NOT_APPROVED", "DAILY_PNL_UNVERIFIED",
                "AVAILABLE_CAPITAL_UNVERIFIED", "EXECUTION_GATE_ABSENT"])),
            "currency": currency, "order_notional": str(notional),
            "orders_enabled": False, "usable_for_risk": False, "usable_for_execution": False}


def assess_risk_readiness(*, draft: dict | None, portfolio: dict | None,
                          valuations: list[dict], reconciliation: dict,
                          order_gate_connected: bool = False) -> dict:
    """Audit actual sources; return blocked facts only. Reject corrupt flags."""
    if type(order_gate_connected) is not bool:
        raise RiskReadinessError("Unknown order gate state.")
    checked = validate_draft(draft)
    reasons = ["REAL_ORDER_EXECUTION_DISABLED", "RISK_POLICY_NOT_APPROVED",
               "RISK_RESERVATIONS_NOT_IMPLEMENTED",
               "RISK_INPUT_LINEAGE_NOT_APPROVED", "DAILY_PNL_UNVERIFIED"]
    evidence = {"draft_candidate_id": None, "draft_sha256": None,
                "portfolio_snapshot_id": None, "portfolio_sha256": None,
                "valuation_id": None, "valuation_sha256": None,
                "reconciliation_sha256": None}
    if checked is None:
        reasons.append("OWNER_LIMITS_MISSING")
    else:
        evidence["draft_candidate_id"] = checked["candidate_id"]
        evidence["draft_sha256"] = checked["payload_sha256"]
        # The three selected amounts do not form a full RiskPolicy. Missing
        # limits and permissions are NEVER filled from balances or defaults.
        reasons.append("OWNER_LIMITS_ARE_DRAFT_ONLY")
        reasons.append("COMPLETE_RISK_POLICY_MISSING")

    if portfolio is None:
        reasons.append("BROKER_PORTFOLIO_MISSING")
    else:
        if (not isinstance(portfolio, dict)
                or portfolio.get("provider") != "Crypto.com Exchange"
                or portfolio.get("authority_status") != "OBSERVATION_ONLY"
                or portfolio.get("usable_for_risk") is not False
                or portfolio.get("usable_for_execution") is not False
                or portfolio.get("orders_enabled") is not False
                or type(portfolio.get("snapshot_id")) is not int
                or portfolio["snapshot_id"] <= 0
                or not isinstance(portfolio.get("stored_payload_sha256"), str)
                or _SHA256.fullmatch(portfolio["stored_payload_sha256"]) is None):
            raise RiskReadinessError("Untrusted broker portfolio observation.")
        _aware(portfolio.get("fetched_at"))
        evidence["portfolio_snapshot_id"] = portfolio["snapshot_id"]
        evidence["portfolio_sha256"] = portfolio["stored_payload_sha256"]
        reasons.append("BROKER_PORTFOLIO_OBSERVATION_ONLY")
        if portfolio.get("positions"):
            reasons.append("DERIVATIVES_OR_MARGIN_NOT_RECONCILED")

    if not isinstance(valuations, list) or len(valuations) > 100:
        raise RiskReadinessError("Untrusted valuation collection.")
    if not valuations:
        reasons.append("VALUATION_EVIDENCE_MISSING")
    else:
        latest = valuations[0]
        if (not isinstance(latest, dict)
                or type(latest.get("valuation_id")) is not int
                or latest["valuation_id"] <= 0
                or latest.get("authority_status") != "BROKER_SPOT_PORTFOLIO_CANDIDATE_ONLY"
                or latest.get("usable_for_risk") is not False
                or latest.get("usable_for_execution") is not False
                or latest.get("orders_enabled") is not False
                or not isinstance(latest.get("stored_payload_sha256"), str)
                or _SHA256.fullmatch(latest["stored_payload_sha256"]) is None):
            raise RiskReadinessError("Untrusted candidate valuation.")
        _aware(latest.get("as_of"))
        evidence["valuation_id"] = latest["valuation_id"]
        evidence["valuation_sha256"] = latest["stored_payload_sha256"]
        reasons.append("VALUATION_CANDIDATE_ONLY")
        if checked is not None and checked["base_currency"] != latest.get("base_currency"):
            reasons.append("VALUATION_CURRENCY_MISMATCH")

    if (not isinstance(reconciliation, dict)
            or reconciliation.get("status") not in {"BLOCKED_INCOMPLETE", "BLOCKED_CONTRADICTORY"}
            or reconciliation.get("authority_status") != "RECONCILIATION_EVIDENCE_ONLY"
            or reconciliation.get("usable_for_pnl") is not False
            or reconciliation.get("usable_for_risk") is not False
            or reconciliation.get("usable_for_execution") is not False
            or reconciliation.get("orders_enabled") is not False
            or reconciliation.get("pnl") is not None
            or not isinstance(reconciliation.get("evidence_sha256"), str)
            or _SHA256.fullmatch(reconciliation["evidence_sha256"]) is None
            or not isinstance(reconciliation.get("reasons"), list)):
        raise RiskReadinessError("Reconciliation evidence is missing or authoritative unexpectedly.")
    evidence["reconciliation_sha256"] = reconciliation["evidence_sha256"]
    reasons.append("PORTFOLIO_RECONCILIATION_" + reconciliation["status"])
    if order_gate_connected:
        # Unexpected runtime wiring is a safety alarm, never approval.
        reasons.append("UNEXPECTED_ORDER_GATE_CONNECTED")
    else:
        reasons.append("ORDER_AUTHORIZATION_GATE_ABSENT")

    result = {
        "status": "BLOCKED", "trading_state": "NOT_READY",
        "authority_status": "READ_ONLY_RISK_PREPARATION",
        "draft_status": "MISSING" if checked is None else "PENDING_OWNER_REVIEW",
        "execution_gate_connected": order_gate_connected,
        "risk_policy_active": False, "risk_approved": False,
        "daily_pnl_verified": False, "available_capital_verified": False,
        "reservations_active": False, "armed": False, "orders_enabled": False,
        "usable_for_risk": False, "usable_for_execution": False,
        "evidence": evidence, "reason_codes": list(dict.fromkeys(reasons)),
    }
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    result["audit_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return result


__all__ = ["RiskReadinessError", "validate_draft",
           "assess_draft_order_cap", "assess_risk_readiness"]
