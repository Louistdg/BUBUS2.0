"""Fail-closed offline review of Crypto.com order evidence and local wiring.

These routines never call a broker, consume approvals, reserve money or send an
order. Event history is synthetic/untrusted until a durable authenticated Core
journal exists. Even a consistent replay is NOT authorization to trade.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9]{1,32}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,20})(?:\.[0-9]{1,20})?\Z")
_STATES = frozenset({"PENDING", "NEW", "ACTIVE", "FILLED", "CANCELED", "REJECTED", "EXPIRED"})
_TERMINAL = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})
_STEPS = frozenset({"ATTEMPT", "RESPONSE_UNCERTAIN", "ACK", "READBACK", "READBACK_FAILED"})
_EVENT_FIELDS = frozenset({
    "step", "client_oid", "intent_sha256", "observed_at",
    "order_id", "status", "cumulative_quantity",
})


class OrderChainAuditError(ValueError):
    """Untrusted order evidence cannot be evaluated."""


def _iso(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 50:
        raise OrderChainAuditError("Order event timestamp is malformed.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise OrderChainAuditError("Order event timestamp is malformed.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OrderChainAuditError("Order event timestamp needs a timezone.")
    return parsed.astimezone(timezone.utc)


def _qty(value: object, *, allow_zero: bool = True) -> Decimal:
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        raise OrderChainAuditError("Order quantity must be a bounded decimal string.")
    try:
        n = Decimal(value)
    except InvalidOperation:
        raise OrderChainAuditError("Order quantity is invalid.") from None
    if not n.is_finite() or n < 0 or (not allow_zero and n == 0):
        raise OrderChainAuditError("Order quantity is invalid.")
    return n


def _identity(value: object) -> str:
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise OrderChainAuditError("Order client_oid must be a canonical UUID.") from None
    if canonical != value:
        raise OrderChainAuditError("Order client_oid must be a canonical UUID.")
    return canonical


def replay_offline_order_evidence(*, intent: dict, events: list[dict]) -> dict:
    """Audit synthetic order timeline. Never calculate whether retry is safe."""
    if (not isinstance(intent, dict) or set(intent) !=
            {"client_oid", "intent_sha256", "quantity"}
            or not isinstance(events, list) or len(events) > 1000):
        raise OrderChainAuditError("Invalid order audit input shape.")
    client_oid = _identity(intent["client_oid"])
    digest = intent["intent_sha256"]
    if not isinstance(digest, str) or _SHA.fullmatch(digest) is None:
        raise OrderChainAuditError("Intent fingerprint is missing.")
    quantity = _qty(intent["quantity"], allow_zero=False)

    conflicts: list[str] = []
    reasons: list[str] = []
    attempted = 0
    acknowledged_id = None
    detail_id = None
    last_quantity = None
    last_status = None
    last_time = None
    unresolved = False
    readbacks = 0

    for event in events:
        if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
            raise OrderChainAuditError("Order journal event has an invalid shape.")
        step = event["step"]
        if step not in _STEPS:
            raise OrderChainAuditError("Unknown order journal event kind.")
        at = _iso(event["observed_at"])
        if last_time is not None and at < last_time:
            conflicts.append("NON_MONOTONIC_EVENT_CLOCK")
        last_time = at
        if event["client_oid"] != client_oid or event["intent_sha256"] != digest:
            conflicts.append("INTENT_OR_CLIENT_OID_CHANGED")
            continue
        order_id, status, raw_quantity = (event["order_id"],
                                          event["status"], event["cumulative_quantity"])
        if step == "ATTEMPT":
            if any(x is not None for x in (order_id, status, raw_quantity)):
                raise OrderChainAuditError("Submission attempt has unexpected detail fields.")
            attempted += 1
            if attempted > 1:
                conflicts.append("DUPLICATE_SUBMISSION_ATTEMPT")
            unresolved = True
        elif step in {"RESPONSE_UNCERTAIN", "READBACK_FAILED"}:
            if any(x is not None for x in (order_id, status, raw_quantity)):
                raise OrderChainAuditError("Uncertain event cannot assert exchange status.")
            if not attempted:
                conflicts.append("ORDER_ATTEMPT_NOT_DURABLY_RECORDED")
            unresolved = True
        elif step == "ACK":
            if (not isinstance(order_id, str) or _ID.fullmatch(order_id) is None
                    or int(order_id) <= 0 or status is not None or raw_quantity is not None):
                raise OrderChainAuditError("Exchange acknowledgement is malformed.")
            if not attempted:
                conflicts.append("ACK_WITHOUT_SUBMISSION_ATTEMPT")
            if acknowledged_id is not None and acknowledged_id != order_id:
                conflicts.append("CONFLICTING_EXCHANGE_ORDER_ID")
            acknowledged_id = order_id
            # An ACK does not establish a fill or that a later readback exists.
            unresolved = True
        else:  # READBACK
            if (not isinstance(order_id, str) or _ID.fullmatch(order_id) is None
                    or int(order_id) <= 0 or status not in _STATES):
                raise OrderChainAuditError("Order readback identity or status is invalid.")
            n = _qty(raw_quantity)
            if status == "FILLED" and n == 0:
                conflicts.append("ZERO_FILL_MARKED_FILLED")
            elif status == "FILLED" and n != quantity:
                conflicts.append("FILLED_QUANTITY_MISMATCH")
            if n > quantity:
                conflicts.append("EXECUTION_EXCEEDS_INTENT")
            if acknowledged_id is not None and order_id != acknowledged_id:
                conflicts.append("ACK_READBACK_ORDER_ID_MISMATCH")
            if detail_id is not None and order_id != detail_id:
                conflicts.append("READBACK_ORDER_ID_CHANGED")
            if last_quantity is not None and n < last_quantity:
                conflicts.append("CUMULATIVE_FILL_REGRESSED")
            if last_status in _TERMINAL and status != last_status:
                conflicts.append("TERMINAL_STATUS_REVERSED")
            if not attempted:
                conflicts.append("READBACK_WITHOUT_LOCAL_ATTEMPT")
            detail_id = order_id
            last_quantity = n
            last_status = status
            readbacks += 1
            # A consistent readback explains a previous ambiguous response,
            # but is not evidence that the overall account/risk is safe.
            unresolved = False

    if not events:
        reasons.append("ORDER_JOURNAL_EMPTY")
    if not attempted and events:
        reasons.append("SUBMISSION_PROOF_MISSING")
    if attempted and readbacks == 0:
        reasons.append("ORDER_READBACK_MISSING")
    if unresolved:
        reasons.append("SUBMISSION_STATE_UNRESOLVED")
    if not conflicts and attempted and readbacks and last_status in {"PENDING", "NEW", "ACTIVE"}:
        reasons.append("EXCHANGE_ORDER_STILL_OPEN")
    if last_status in _TERMINAL and last_quantity is not None and last_quantity > 0:
        reasons.append("EXECUTED_QUANTITY_MUST_RECONCILE_WITH_BALANCES")
    reasons.extend((
        "AUTHENTICATED_PROVIDER_EVIDENCE_MISSING",
        "DURABLE_EXACT_INTENT_LEDGER_MISSING",
        "SINGLE_USE_APPROVAL_GATE_MISSING",
        "ATOMIC_EXPOSURE_RESERVATIONS_MISSING",
        "CROSS_RESTART_REPLAY_PROTECTION_UNPROVEN",
    ))
    reasons.extend(conflicts)
    reasons = list(dict.fromkeys(reasons))
    report = {
        "status": ("BLOCKED_CONTRADICTORY" if conflicts else
                   "BLOCKED_UNCERTAIN" if unresolved else "BLOCKED_INCOMPLETE"),
        "authority_status": "SYNTHETIC_OFFLINE_ORDER_AUDIT_ONLY",
        "client_oid": client_oid, "intent_sha256": digest,
        "attempt_count": attempted, "readback_count": readbacks,
        "exchange_order_id_observed": detail_id,
        "last_exchange_status_observed": last_status,
        "cumulative_quantity_observed": (None if last_quantity is None
                                        else format(last_quantity, "f")),
        "needs_manual_readback": unresolved, "reasons": reasons,
        "can_retry_automatically": False, "can_submit": False,
        "orders_enabled": False, "usable_for_risk": False,
        "usable_for_execution": False,
    }
    encoded = json.dumps({"intent": intent, "events": events, "report": report},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    report["audit_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return report


def audit_order_chain_configuration(*, gate_connected: bool,
                                    spot_verifier_connected: bool,
                                    risk_readiness: dict,
                                    durable_journal_ready: bool = False,
                                    manual_readback_route_connected: bool = False) -> dict:
    """Report readiness of BUBUS2.0 with no runtime execution capability."""
    if (type(gate_connected) is not bool or type(spot_verifier_connected) is not bool
            or type(durable_journal_ready) is not bool
            or type(manual_readback_route_connected) is not bool):
        raise OrderChainAuditError("Order adapter configuration is invalid.")
    if (not isinstance(risk_readiness, dict)
            or risk_readiness.get("status") != "BLOCKED"
            or risk_readiness.get("armed") is not False
            or risk_readiness.get("orders_enabled") is not False
            or risk_readiness.get("risk_policy_active") is not False
            or risk_readiness.get("usable_for_execution") is not False):
        raise OrderChainAuditError("Risk readiness unexpectedly claims authority.")
    reasons = ["RISK_NOT_READY",
               "ATOMIC_APPROVAL_CONSUMPTION_NOT_IMPLEMENTED"]
    if not manual_readback_route_connected:
        reasons.append("UNCERTAIN_POST_RESPONSE_RECOVERY_NOT_IMPLEMENTED")
    reasons.extend((
               "ORDER_FILL_ACCOUNT_RECONCILIATION_MISSING",
               "REAL_ORDER_ROUTES_DISABLED"))
    if not durable_journal_ready:
        reasons.append("DURABLE_ORDER_JOURNAL_MISSING")
        reasons.append("CROSS_RESTART_IDEMPOTENCY_UNVERIFIED")
    if not gate_connected:
        reasons.append("ORDER_GATE_NOT_CONNECTED")
    else:
        reasons.append("UNEXPECTED_ORDER_GATE_CONNECTED")
    if not spot_verifier_connected:
        reasons.append("SPOT_INSTRUMENT_VERIFIER_MISSING")
    report = {
        "status": "BLOCKED", "authority_status": "ORDER_CHAIN_READ_ONLY_AUDIT",
        "risk_approved": False, "order_gate_connected": gate_connected,
        "spot_verifier_connected": spot_verifier_connected,
        "durable_journal_ready": durable_journal_ready, "retry_automatically": False,
        "manual_readback_available": manual_readback_route_connected,
        "recovery_ready": False, "can_submit": False,
        "armed": False, "orders_enabled": False,
        "usable_for_risk": False, "usable_for_execution": False,
        "reasons": reasons,
    }
    report["audit_sha256"] = hashlib.sha256(json.dumps(
        report, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return report


__all__ = ["OrderChainAuditError", "replay_offline_order_evidence",
           "audit_order_chain_configuration"]
