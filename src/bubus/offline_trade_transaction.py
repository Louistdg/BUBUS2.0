"""One-database atomic rehearsal linking an offline order and synthetic Risk.

This is a test/rehearsal boundary only. It never creates a real Risk receipt,
never returns an execution authorization, and never talks to Crypto.com.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

from bubus.offline_risk_reservations import (
    OfflineRiskError,
    OfflineRiskReservations,
    _dump,
    _id,
    _intent_doc,
    _parse_time,
    _sha,
)
from bubus.order_journal import (
    OfflineOrderJournal,
    OrderJournalError,
    _AUTHORITY as _ORDER_AUTHORITY,
    _json,
    _order_document,
)
from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder


class OfflineTradeTransactionError(ValueError):
    """Invalid or inconsistent rehearsal transaction; the entire write rolls back."""


class OfflineTradeTransaction:
    """Coordinate two existing ledgers that share exactly one SQLite database."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None):
        self.path = Path(path).resolve()
        self.risk = OfflineRiskReservations(self.path, clock=clock)
        self.orders = OfflineOrderJournal(self.path, clock=clock)

    def record_and_reserve(self, *, approval_id: str,
                           intent: CryptoComSpotLimitOrder) -> dict:
        """Atomically insert intent, consume its fixture approval, and reserve funds."""
        try:
            _id(approval_id, "approval_id")
            if not isinstance(intent, CryptoComSpotLimitOrder):
                raise OfflineTradeTransactionError("Validated synthetic Spot intent required.")
            if _order_document(intent) != _intent_doc(intent):
                raise OfflineTradeTransactionError("Order intent does not match Risk canonical form.")

            with self.risk._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    budget, approvals, reserved = self.risk._checked(db)
                    if budget is None:
                        raise OfflineTradeTransactionError("No explicit synthetic budget exists.")
                    approval = next((row for row in approvals if row["approval_id"] == approval_id), None)
                    if approval is None:
                        raise OfflineTradeTransactionError("Synthetic approval not found.")
                    if approval["state"] != "PENDING" or approval["consumed_at"] is not None:
                        raise OfflineTradeTransactionError("Synthetic approval already consumed.")
                    if (approval["client_oid"] != intent.client_oid
                            or approval["intent_sha256"] != intent.canonical_sha256()
                            or json.loads(approval["intent_json"]) != _intent_doc(intent)):
                        raise OfflineTradeTransactionError("Intent differs from its synthetic approval.")
                    now = self.risk._now()
                    stamp = now.isoformat()
                    if not (_parse_time(approval["approved_at"]) <= now < _parse_time(approval["expires_at"])):
                        raise OfflineTradeTransactionError("Synthetic approval expired or clock regressed.")
                    if budget[4] >= budget[3]:
                        raise OfflineTradeTransactionError("Synthetic daily-loss cap reached.")
                    amount = Decimal(approval["reservation_amount"])
                    if amount > budget[2] or amount > budget[1] - reserved:
                        raise OfflineTradeTransactionError("Insufficient synthetic capital.")

                    existing = db.execute(
                        "SELECT 1 FROM offline_order_intents WHERE client_oid=?",
                        (intent.client_oid,),
                    ).fetchone()
                    if existing:
                        raise OfflineTradeTransactionError("client_oid already exists in the order journal.")
                    cursor = db.execute("""INSERT INTO offline_order_intents
                        (client_oid,intent_json,intent_sha256,created_at,authority_status)
                        VALUES(?,?,?,?,?)""",
                        (intent.client_oid, _json(_order_document(intent)), intent.canonical_sha256(),
                         stamp, _ORDER_AUTHORITY))
                    order_row = db.execute(
                        "SELECT * FROM offline_order_intents WHERE id=?", (cursor.lastrowid,)
                    ).fetchone()
                    self.orders._append(db, order_row, "INTENT_RECORDED", {}, stamp)

                    update = db.execute("""UPDATE synthetic_risk_approvals
                        SET state='RESERVED', consumed_at=?
                        WHERE id=? AND state='PENDING' AND consumed_at IS NULL""",
                        (stamp, approval["id"]))
                    if update.rowcount != 1:
                        raise OfflineTradeTransactionError("Synthetic approval was consumed concurrently.")
                    receipt_material = {
                        "approval_id": approval_id, "approval_sha256": approval["approval_sha256"],
                        "intent_sha256": approval["intent_sha256"],
                        "amount": approval["reservation_amount"], "consumed_at": stamp,
                    }
                    consumption_sha = _sha(_dump(receipt_material))
                    db.execute("""INSERT INTO synthetic_risk_consumptions
                        (approval_id,consumed_at,consumption_sha256) VALUES(?,?,?)""",
                        (approval_id, stamp, consumption_sha))
                    link = {
                        "client_oid": intent.client_oid,
                        "intent_sha256": intent.canonical_sha256(),
                        "approval_id": approval_id,
                        "approval_sha256": approval["approval_sha256"],
                        "consumption_sha256": consumption_sha,
                        "quote_currency": approval["quote_currency"],
                        "reservation_amount": approval["reservation_amount"],
                    }
                    transaction_sha = hashlib.sha256(
                        _json(link).encode("utf-8")
                    ).hexdigest()
                    event = {
                        "approval_id": approval_id,
                        "approval_sha256": approval["approval_sha256"],
                        "consumption_sha256": consumption_sha,
                        "quote_currency": approval["quote_currency"],
                        "reservation_amount": approval["reservation_amount"],
                        "transaction_sha256": transaction_sha,
                    }
                    order_row = self.orders._row(db, intent.client_oid)
                    self.orders._append(db, order_row, "SIMULATED_RISK_RESERVED", event, stamp)

                    # Verify both complete ledgers before SQLite commits either one.
                    risk_budget, checked_approvals, total_after = self.risk._checked(db)
                    if risk_budget is None or total_after != reserved + amount:
                        raise OfflineTradeTransactionError("Combined reservation ledger failed verification.")
                    final_approval = next(row for row in checked_approvals
                                          if row["approval_id"] == approval_id)
                    order_row = self.orders._row(db, intent.client_oid)
                    order_entry = self.orders._checked_entry(db, order_row)
                    if (final_approval["state"] != "RESERVED"
                            or order_entry["risk_reservation"] != event):
                        raise OfflineTradeTransactionError("Combined journal receipt did not verify.")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                else:
                    db.execute("COMMIT")
            return self.audit()
        except (OfflineRiskError, OrderJournalError, sqlite3.Error, ValueError, TypeError) as exc:
            if isinstance(exc, OfflineTradeTransactionError):
                raise
            raise OfflineTradeTransactionError(str(exc)) from None

    def audit(self) -> dict:
        with self.risk._connect() as db:
            db.execute("BEGIN")
            try:
                _, approval_rows, _ = self.risk._checked(db)
                order_rows = db.execute(
                    "SELECT * FROM offline_order_intents ORDER BY id DESC"
                ).fetchall()
                if len(order_rows) > 10000:
                    raise OfflineTradeTransactionError("Combined rehearsal exceeds its verified row bound.")
                order_entries = [self.orders._checked_entry(db, row) for row in order_rows]
                receipts = {row["approval_id"]: row for row in db.execute(
                    "SELECT approval_id,consumed_at,consumption_sha256 FROM synthetic_risk_consumptions"
                ).fetchall()}
            finally:
                db.execute("ROLLBACK")
        approvals = {row["approval_id"]: row for row in approval_rows}
        linked = []
        for order in order_entries:
            proof = order["risk_reservation"]
            if proof is None:
                continue
            approval = approvals.get(proof["approval_id"])
            receipt = receipts.get(proof["approval_id"])
            if (approval is None or approval["client_oid"] != order["client_oid"]
                    or approval["intent_sha256"] != order["intent_sha256"]
                    or approval["approval_sha256"] != proof["approval_sha256"]
                    or approval["reservation_amount"] != proof["reservation_amount"]
                    or approval["quote_currency"] != proof["quote_currency"]
                    or approval["state"] != "RESERVED"
                    or receipt is None
                    or receipt["consumption_sha256"] != proof["consumption_sha256"]
                    or receipt["consumed_at"] != approval["consumed_at"]):
                raise OfflineTradeTransactionError("Cross-ledger reservation proof is inconsistent.")
            linked.append({
                "client_oid": order["client_oid"], "approval_id": proof["approval_id"],
                "transaction_sha256": proof["transaction_sha256"],
                "reservation_amount": proof["reservation_amount"],
                "quote_currency": proof["quote_currency"],
                "consumed_at": approval["consumed_at"],
            })
        report = {
            "status": "BLOCKED_SIMULATION_ONLY",
            "authority_status": "SYNTHETIC_CROSS_LEDGER_REHEARSAL_ONLY",
            "single_sqlite_database": True,
            "atomic_order_risk_transaction": True,
            "all_rows_verified": True,
            "total_order_intents": len(order_entries),
            "verified_linked_transactions": len(linked),
            "linked_transactions": linked,
            "orders_enabled": False, "can_submit": False,
            "usable_for_risk": False, "usable_for_execution": False,
            "real_human_approval": False, "connected_to_live_adapter": False,
        }
        return report


__all__ = ["OfflineTradeTransaction", "OfflineTradeTransactionError"]
