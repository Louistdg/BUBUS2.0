"""Durable, append-only evidence for real Crypto.com Spot order attempts.

This journal does not authorize an order. A Risk/Policy gate is still required;
it prevents a client order identifier from being reused after a restart and
records ambiguous submissions so callers must reconcile before proceeding.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from invest_ai.execution.cryptocom_spot import CryptoComOrderStatus, CryptoComSpotLimitOrder

_GENESIS = "0" * 64
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_TYPES = frozenset({
    "INTENT_RESERVED", "AUTHORIZATION_CONSUMED", "AUTHORIZATION_REJECTED",
    "SUBMISSION_ATTEMPTED", "SUBMISSION_UNCERTAIN", "ACK_OBSERVED",
    "ORDER_READBACK_OBSERVED",
})
_STATES = frozenset({"RESERVED", "AUTHORIZED", "REJECTED", "ATTEMPTED", "UNCERTAIN", "ACKNOWLEDGED"})
_OPEN_STATUSES = frozenset({"PENDING", "NEW", "ACTIVE"})
_FINAL_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})
_ALL_STATUSES = _OPEN_STATUSES | _FINAL_STATUSES
_MAX_DECIMAL_DIGITS = 40


class LiveOrderJournalError(ValueError):
    """Duplicate intent, invalid transition, or journal integrity failure."""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LiveOrderJournalError("A timezone-aware journal clock is required.")
    return value.astimezone(timezone.utc)


def _intent_document(intent: CryptoComSpotLimitOrder) -> dict[str, str]:
    if not isinstance(intent, CryptoComSpotLimitOrder):
        raise LiveOrderJournalError("Only a validated Crypto.com Spot LIMIT intent can be journaled.")
    return {
        "client_oid": intent.client_oid,
        "instrument_name": intent.instrument_name,
        "order_type": "LIMIT",
        "price": format(intent.price, "f"),
        "quantity": format(intent.quantity, "f"),
        "side": intent.side,
        "time_in_force": "FILL_OR_KILL",
    }


def _event_hash(client_oid: str, seq: int, kind: str, at: str,
                previous: str, payload_hash: str) -> str:
    return _sha(_json({
        "client_oid": client_oid, "seq": seq, "kind": kind, "at": at,
        "previous": previous, "payload_sha256": payload_hash,
    }))


class LiveOrderJournal:
    """Production journal for order intents; never creates orders or approvals."""

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise LiveOrderJournalError("Unrecognized live-order journal schema.")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS live_order_intents (
                    client_oid TEXT PRIMARY KEY,
                    intent_json TEXT NOT NULL,
                    intent_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('RESERVED','AUTHORIZED','REJECTED','ATTEMPTED','UNCERTAIN','ACKNOWLEDGED')),
                    created_at TEXT NOT NULL,
                    event_count INTEGER NOT NULL CHECK(event_count >= 0),
                    event_head_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_order_events (
                    client_oid TEXT NOT NULL REFERENCES live_order_intents(client_oid),
                    seq INTEGER NOT NULL CHECK(seq >= 1),
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    previous_sha256 TEXT NOT NULL,
                    event_sha256 TEXT NOT NULL,
                    PRIMARY KEY(client_oid,seq)
                );
                CREATE TRIGGER IF NOT EXISTS live_order_intents_identity_immutable
                BEFORE UPDATE OF client_oid,intent_json,intent_sha256,created_at ON live_order_intents
                BEGIN SELECT RAISE(ABORT,'live order intent identity is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS live_order_intents_no_delete
                BEFORE DELETE ON live_order_intents
                BEGIN SELECT RAISE(ABORT,'live order intent is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS live_order_events_no_update
                BEFORE UPDATE ON live_order_events
                BEGIN SELECT RAISE(ABORT,'live order event is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS live_order_events_no_delete
                BEFORE DELETE ON live_order_events
                BEGIN SELECT RAISE(ABORT,'live order event is immutable'); END;
            """)
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    def _append(self, db: sqlite3.Connection, client_oid: str, kind: str,
                payload: dict[str, object], state: str) -> None:
        row = db.execute("SELECT event_count,event_head_sha256 FROM live_order_intents WHERE client_oid=?",
                         (client_oid,)).fetchone()
        if row is None:
            raise LiveOrderJournalError("Order intent is missing from the durable journal.")
        seq = int(row[0]) + 1
        previous = str(row[1])
        stamp = _utc(self._clock()).isoformat(timespec="microseconds").replace("+00:00", "Z")
        payload_text = _json(payload)
        payload_hash = _sha(payload_text)
        digest = _event_hash(client_oid, seq, kind, stamp, previous, payload_hash)
        db.execute("INSERT INTO live_order_events VALUES(?,?,?,?,?,?,?,?)",
                   (client_oid, seq, kind, stamp, payload_text, payload_hash, previous, digest))
        db.execute("UPDATE live_order_intents SET state=?,event_count=?,event_head_sha256=? WHERE client_oid=?",
                   (state, seq, digest, client_oid))

    def reserve_intent(self, intent: CryptoComSpotLimitOrder) -> None:
        document = _intent_document(intent)
        encoded = _json(document)
        digest = intent.canonical_sha256()
        stamp = _utc(self._clock()).isoformat(timespec="microseconds").replace("+00:00", "Z")
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT intent_sha256 FROM live_order_intents WHERE client_oid=?",
                                      (intent.client_oid,)).fetchone()
                if existing is not None:
                    raise LiveOrderJournalError("client_oid already exists in the durable order journal; reconcile it.")
                db.execute("INSERT INTO live_order_intents VALUES(?,?,?,?,?,?,?)",
                           (intent.client_oid, encoded, digest, "RESERVED", stamp, 0, _GENESIS))
                self._append(db, intent.client_oid, "INTENT_RESERVED", {
                    "intent_sha256": digest,
                }, "RESERVED")
                db.execute("COMMIT")
        except sqlite3.IntegrityError:
            raise LiveOrderJournalError("client_oid was concurrently reserved; reconcile it.") from None
        except BaseException:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def record_authorization(self, client_oid: str, *, risk_receipt_id: str,
                             policy_revision: str, control_epoch: int,
                             human_approval_id: str) -> None:
        values = (risk_receipt_id, policy_revision, human_approval_id)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise LiveOrderJournalError("Authorization evidence is incomplete.")
        if type(control_epoch) is not int or control_epoch < 0:
            raise LiveOrderJournalError("Authorization control epoch is invalid.")
        self._transition(client_oid, {"RESERVED"}, "AUTHORIZATION_CONSUMED", {
            "risk_receipt_id": risk_receipt_id, "policy_revision": policy_revision,
            "control_epoch": control_epoch, "human_approval_id": human_approval_id,
        }, "AUTHORIZED")

    def record_authorization_rejected(self, client_oid: str, reason_code: str) -> None:
        self._reason(reason_code)
        self._transition(client_oid, {"RESERVED"}, "AUTHORIZATION_REJECTED",
                         {"reason_code": reason_code}, "REJECTED")

    def record_submission_attempt(self, client_oid: str, intent_sha256: str) -> None:
        if not isinstance(intent_sha256, str) or _SHA256.fullmatch(intent_sha256) is None:
            raise LiveOrderJournalError("Order intent digest is invalid.")
        self._transition(client_oid, {"AUTHORIZED"}, "SUBMISSION_ATTEMPTED",
                         {"intent_sha256": intent_sha256}, "ATTEMPTED")

    def record_submission_uncertain(self, client_oid: str, reason_code: str) -> None:
        self._reason(reason_code)
        self._transition(client_oid, {"ATTEMPTED"}, "SUBMISSION_UNCERTAIN",
                         {"reason_code": reason_code}, "UNCERTAIN")

    def record_acknowledgement(self, client_oid: str, *, order_id: str,
                               echoed_client_oid: str) -> None:
        if (not isinstance(order_id, str) or not order_id.isdecimal() or int(order_id) <= 0
                or echoed_client_oid != client_oid):
            raise LiveOrderJournalError("Exchange acknowledgement identity is invalid.")
        self._transition(client_oid, {"ATTEMPTED"}, "ACK_OBSERVED", {
            "order_id": order_id, "client_oid": echoed_client_oid,
        }, "ACKNOWLEDGED")

    def contains_intent(self, client_oid: str) -> bool:
        with self._connection() as db:
            row = db.execute("SELECT * FROM live_order_intents WHERE client_oid=?", (client_oid,)).fetchone()
            if row is None:
                return False
            self._verify_intent(db, row)
            return True

    def record_order_readback(self, status: CryptoComOrderStatus) -> None:
        payload = self._readback_document(status)
        client_oid = payload["client_oid"]
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM live_order_intents WHERE client_oid=?", (client_oid,)).fetchone()
                if row is None:
                    raise LiveOrderJournalError("Order readback has no matching durable intent.")
                state = self._verify_intent(db, row)
                if state not in {"ATTEMPTED", "UNCERTAIN", "ACKNOWLEDGED"}:
                    raise LiveOrderJournalError("Order journal state does not accept a readback.")
                intent = json.loads(row["intent_json"])
                if Decimal(payload["executed_quantity"]) > Decimal(intent["quantity"]):
                    raise LiveOrderJournalError("Exchange execution exceeds the durable order quantity.")
                events = db.execute("SELECT event_type,payload_json FROM live_order_events "
                                    "WHERE client_oid=? ORDER BY seq", (client_oid,)).fetchall()
                prior_order_ids = []
                prior_readbacks = []
                for event in events:
                    event_payload = json.loads(event["payload_json"])
                    if event["event_type"] == "ACK_OBSERVED":
                        prior_order_ids.append(event_payload["order_id"])
                    elif event["event_type"] == "ORDER_READBACK_OBSERVED":
                        prior_order_ids.append(event_payload["order_id"])
                        prior_readbacks.append(event_payload)
                if prior_order_ids and any(value != payload["order_id"] for value in prior_order_ids):
                    raise LiveOrderJournalError("Readback order ID conflicts with earlier exchange evidence.")
                if prior_readbacks:
                    if not self._readback_is_monotone(prior_readbacks[-1], payload):
                        raise LiveOrderJournalError("Order execution or status regressed in the readback.")
                self._append(db, client_oid, "ORDER_READBACK_OBSERVED", payload, state)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _readback_document(status: CryptoComOrderStatus) -> dict[str, object]:
        if not isinstance(status, CryptoComOrderStatus):
            raise LiveOrderJournalError("A validated Crypto.com order status is required.")
        if (not isinstance(status.order_id, str) or not status.order_id.isdecimal()
                or int(status.order_id) <= 0 or not isinstance(status.client_oid, str)
                or not isinstance(status.status, str) or status.status not in _ALL_STATUSES):
            raise LiveOrderJournalError("Crypto.com order readback identity or status is invalid.")
        values = {}
        for name in ("executed_quantity", "executed_value", "fees"):
            value = getattr(status, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise LiveOrderJournalError(f"Order readback {name} must be a finite non-negative Decimal.")
            if len(value.as_tuple().digits) > _MAX_DECIMAL_DIGITS or abs(value.as_tuple().exponent) > _MAX_DECIMAL_DIGITS:
                raise LiveOrderJournalError(f"Order readback {name} exceeds supported bounds.")
            values[name] = format(value, "f")
        if status.status == "FILLED" and status.executed_quantity <= 0:
            raise LiveOrderJournalError("FILLED status cannot report zero cumulative quantity.")
        if status.fee_currency is not None and (
                not isinstance(status.fee_currency, str)
                or re.fullmatch(r"[A-Z0-9]{2,16}", status.fee_currency) is None):
            raise LiveOrderJournalError("Order readback fee currency is invalid.")
        updated_at = None
        if status.updated_at is not None:
            updated_at = _utc(status.updated_at).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return {
            "order_id": status.order_id, "client_oid": status.client_oid, "status": status.status,
            **values, "fee_currency": status.fee_currency, "updated_at": updated_at,
        }

    def _transition(self, client_oid: str, expected: set[str], event: str,
                    payload: dict[str, object], state: str) -> None:
        if not isinstance(client_oid, str) or not client_oid:
            raise LiveOrderJournalError("client_oid is required.")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM live_order_intents WHERE client_oid=?",
                                 (client_oid,)).fetchone()
                if row is None or self._verify_intent(db, row) not in expected:
                    raise LiveOrderJournalError("Order journal state does not allow this transition.")
                self._append(db, client_oid, event, payload, state)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _reason(value: str) -> None:
        if not isinstance(value, str) or re.fullmatch(r"[A-Z0-9_]{1,64}", value) is None:
            raise LiveOrderJournalError("A canonical reason code is required.")

    def audit(self, client_oid: str | None = None) -> dict[str, object]:
        with self._connection() as db:
            if client_oid is None:
                intents = db.execute("SELECT * FROM live_order_intents ORDER BY created_at,client_oid").fetchall()
            else:
                intents = db.execute("SELECT * FROM live_order_intents WHERE client_oid=?", (client_oid,)).fetchall()
            rows = []
            for intent in intents:
                derived_state = self._verify_intent(db, intent)
                events = db.execute("SELECT event_type,payload_json FROM live_order_events WHERE client_oid=? ORDER BY seq",
                                    (intent["client_oid"],)).fetchall()
                readbacks = [json.loads(row["payload_json"]) for row in events
                             if row["event_type"] == "ORDER_READBACK_OBSERVED"]
                acknowledgements = [json.loads(row["payload_json"]) for row in events
                                    if row["event_type"] == "ACK_OBSERVED"]
                latest_readback = readbacks[-1] if readbacks else None
                rows.append({
                    "client_oid": intent["client_oid"], "intent_sha256": intent["intent_sha256"],
                    "state": intent["state"], "event_count": len(events),
                    "event_head_sha256": intent["event_head_sha256"],
                    "last_event": events[-1]["event_type"], "readback_count": len(readbacks),
                    "submission_uncertain": any(row["event_type"] == "SUBMISSION_UNCERTAIN" for row in events),
                    "exchange_order_id": (latest_readback["order_id"] if latest_readback else
                                          acknowledgements[-1]["order_id"] if acknowledgements else None),
                    "exchange_status": latest_readback["status"] if latest_readback else None,
                    "executed_quantity": latest_readback["executed_quantity"] if latest_readback else None,
                    "executed_value": latest_readback["executed_value"] if latest_readback else None,
                    "updated_at": latest_readback["updated_at"] if latest_readback else None,
                })
        report = {"status": "INTEGRITY_OK", "authority_status": "LIVE_ORDER_JOURNAL_EVIDENCE_ONLY",
                  "orders_enabled": False, "intents": rows}
        report["audit_sha256"] = _sha(_json(report))
        return report

    def _verify_intent(self, db: sqlite3.Connection, intent: sqlite3.Row) -> str:
        try:
            doc = json.loads(intent["intent_json"])
            parsed = CryptoComSpotLimitOrder(
                client_oid=doc["client_oid"], instrument_name=doc["instrument_name"],
                side=doc["side"], price=Decimal(doc["price"]), quantity=Decimal(doc["quantity"]),
            )
        except (KeyError, TypeError, ValueError, ArithmeticError, json.JSONDecodeError):
            raise LiveOrderJournalError("Stored live order intent is malformed.") from None
        if (_json(_intent_document(parsed)) != intent["intent_json"]
                or parsed.canonical_sha256() != intent["intent_sha256"]):
            raise LiveOrderJournalError("Stored live order intent failed integrity verification.")
        events = db.execute("SELECT * FROM live_order_events WHERE client_oid=? ORDER BY seq",
                            (intent["client_oid"],)).fetchall()
        previous, last_event, derived_state = _GENESIS, None, None
        acknowledged_order_id = None
        previous_readback = None
        transitions = {
            ("RESERVED", "AUTHORIZATION_CONSUMED"): "AUTHORIZED",
            ("RESERVED", "AUTHORIZATION_REJECTED"): "REJECTED",
            ("AUTHORIZED", "SUBMISSION_ATTEMPTED"): "ATTEMPTED",
            ("ATTEMPTED", "SUBMISSION_UNCERTAIN"): "UNCERTAIN",
            ("ATTEMPTED", "ACK_OBSERVED"): "ACKNOWLEDGED",
        }
        for expected_seq, event in enumerate(events, start=1):
            payload_text = event["payload_json"]
            try:
                payload = json.loads(payload_text)
            except (TypeError, ValueError):
                raise LiveOrderJournalError("Live order event payload is malformed.") from None
            if (event["seq"] != expected_seq or event["event_type"] not in _EVENT_TYPES
                    or _sha(payload_text) != event["payload_sha256"]
                    or event["previous_sha256"] != previous
                    or _event_hash(intent["client_oid"], expected_seq, event["event_type"],
                                   event["created_at"], previous, event["payload_sha256"])
                       != event["event_sha256"]):
                raise LiveOrderJournalError("Live order event chain failed integrity verification.")
            previous, last_event = event["event_sha256"], event["event_type"]
            if expected_seq == 1:
                if (last_event != "INTENT_RESERVED"
                        or payload != {"intent_sha256": intent["intent_sha256"]}):
                    raise LiveOrderJournalError("Live order journal genesis event is invalid.")
                derived_state = "RESERVED"
            else:
                if last_event == "ORDER_READBACK_OBSERVED":
                    if derived_state not in {"ATTEMPTED", "UNCERTAIN", "ACKNOWLEDGED"}:
                        raise LiveOrderJournalError("Live order journal contains an invalid readback transition.")
                else:
                    derived_state = transitions.get((derived_state, last_event))
                    if derived_state is None:
                        raise LiveOrderJournalError("Live order journal contains an invalid state transition.")
                if last_event == "AUTHORIZATION_CONSUMED":
                    if (not isinstance(payload, dict) or set(payload) != {
                            "risk_receipt_id", "policy_revision", "control_epoch", "human_approval_id"}
                            or any(not isinstance(payload[name], str) or not payload[name].strip()
                                   for name in ("risk_receipt_id", "policy_revision", "human_approval_id"))
                            or type(payload["control_epoch"]) is not int or payload["control_epoch"] < 0):
                        raise LiveOrderJournalError("Stored authorization event is invalid.")
                elif last_event == "AUTHORIZATION_REJECTED":
                    if (not isinstance(payload, dict) or set(payload) != {"reason_code"}
                            or not isinstance(payload["reason_code"], str)
                            or re.fullmatch(r"[A-Z0-9_]{1,64}", payload["reason_code"]) is None):
                        raise LiveOrderJournalError("Stored authorization refusal is invalid.")
                elif last_event == "SUBMISSION_ATTEMPTED":
                    if payload != {"intent_sha256": intent["intent_sha256"]}:
                        raise LiveOrderJournalError("Stored submission attempt does not match its intent.")
                elif last_event == "SUBMISSION_UNCERTAIN":
                    if (not isinstance(payload, dict) or set(payload) != {"reason_code"}
                            or not isinstance(payload["reason_code"], str)
                            or re.fullmatch(r"[A-Z0-9_]{1,64}", payload["reason_code"]) is None):
                        raise LiveOrderJournalError("Stored uncertain submission reason is invalid.")
                elif last_event == "ACK_OBSERVED":
                    if (not isinstance(payload, dict) or set(payload) != {"order_id", "client_oid"}
                            or not isinstance(payload["order_id"], str)
                            or not payload["order_id"].isdecimal() or int(payload["order_id"]) <= 0
                            or payload["client_oid"] != intent["client_oid"]):
                        raise LiveOrderJournalError("Stored exchange acknowledgement is invalid.")
                    acknowledged_order_id = payload["order_id"]
                elif last_event == "ORDER_READBACK_OBSERVED":
                    readback = self._parse_readback_payload(payload)
                    if (acknowledged_order_id is not None
                            and readback["order_id"] != acknowledged_order_id):
                        raise LiveOrderJournalError("Stored readback conflicts with the exchange acknowledgement.")
                    if (previous_readback is not None
                            and not self._readback_is_monotone(previous_readback, readback)):
                        raise LiveOrderJournalError("Stored order readbacks are not monotone.")
                    previous_readback = readback
        if intent["state"] in _STATES and intent["state"] != derived_state:
            raise LiveOrderJournalError("Live order journal state differs from its event history.")
        if (not events or len(events) != intent["event_count"]
                or previous != intent["event_head_sha256"] or intent["state"] not in _STATES):
            raise LiveOrderJournalError("Live order journal head or state failed integrity verification.")
        return derived_state

    @classmethod
    def _parse_readback_payload(cls, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or set(payload) != {
                "order_id", "client_oid", "status", "executed_quantity", "executed_value",
                "fees", "fee_currency", "updated_at"}:
            raise LiveOrderJournalError("Stored order readback payload shape is invalid.")
        try:
            updated_at = None if payload["updated_at"] is None else datetime.fromisoformat(
                str(payload["updated_at"]).replace("Z", "+00:00"))
            status = CryptoComOrderStatus(
                order_id=payload["order_id"], client_oid=payload["client_oid"], status=payload["status"],
                executed_quantity=Decimal(payload["executed_quantity"]),
                executed_value=Decimal(payload["executed_value"]), fees=Decimal(payload["fees"]),
                updated_at=updated_at, fee_currency=payload["fee_currency"],
            )
            normalized = cls._readback_document(status)
        except (TypeError, ValueError, ArithmeticError, OverflowError):
            raise LiveOrderJournalError("Stored order readback is invalid.") from None
        if normalized != payload:
            raise LiveOrderJournalError("Stored order readback is not canonical.")
        return normalized

    @staticmethod
    def _readback_is_monotone(previous: dict[str, object], current: dict[str, object]) -> bool:
        if previous["order_id"] != current["order_id"]:
            return False
        for field in ("executed_quantity", "executed_value", "fees"):
            if Decimal(current[field]) < Decimal(previous[field]):
                return False
        if previous["status"] in _FINAL_STATUSES and current["status"] != previous["status"]:
            return False
        if (previous["updated_at"] is not None and current["updated_at"] is not None
                and current["updated_at"] < previous["updated_at"]):
            return False
        return True


__all__ = ["LiveOrderJournal", "LiveOrderJournalError"]
