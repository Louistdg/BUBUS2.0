"""Durable SQLite rehearsal journal for Crypto.com Spot order intentions.

NO live transport, keys, order authorization, approvals or trading capability.
Records are entirely synthetic and deliberately cannot be used by a live
execution path. SQLite serializes writers and holds unique client_oid values
across process restarts. This does NOT prove provider-side idempotency.
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

from bubus.order_chain_audit import replay_offline_order_evidence
from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder

_AUTHORITY = "SYNTHETIC_ORDER_JOURNAL_ONLY"
_GENESIS = "0" * 64
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ORDER_ID = re.compile(r"[0-9]{1,32}\Z")
_STATES = frozenset({"PENDING", "NEW", "ACTIVE", "FILLED", "CANCELED", "REJECTED", "EXPIRED"})
_EVENT_TYPES = frozenset({
    "INTENT_RECORDED", "SIMULATED_ATTEMPT_RESERVED", "RESPONSE_UNCERTAIN",
    "ACK_OBSERVED", "READBACK_OBSERVED", "READBACK_FAILED",
    "SIMULATED_RISK_RESERVED",
})
_MAX_EVENTS = 512


class OrderJournalError(ValueError):
    """Unsafe duplicate, invalid transition or journal integrity failure."""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OrderJournalError("An aware UTC clock is required.")
    return value.astimezone(timezone.utc)


def _order_document(intent: CryptoComSpotLimitOrder) -> dict:
    if not isinstance(intent, CryptoComSpotLimitOrder):
        raise OrderJournalError("Only validated Crypto.com Spot limit intentions are supported.")
    return {
        "client_oid": intent.client_oid, "instrument_name": intent.instrument_name,
        "order_type": "LIMIT", "price": format(intent.price, "f"),
        "quantity": format(intent.quantity, "f"), "side": intent.side,
        "time_in_force": "FILL_OR_KILL",
    }


def _verify_document(doc: object, intent_hash: object, oid: object) -> dict:
    if not isinstance(doc, dict) or set(doc) != {
        "client_oid", "instrument_name", "order_type", "price", "quantity",
        "side", "time_in_force",
    } or doc.get("order_type") != "LIMIT" or doc.get("time_in_force") != "FILL_OR_KILL":
        raise OrderJournalError("Stored synthetic order intent shape is invalid.")
    if not isinstance(intent_hash, str) or _SHA256.fullmatch(intent_hash) is None:
        raise OrderJournalError("Stored order intent fingerprint is invalid.")
    try:
        intent = CryptoComSpotLimitOrder(
            client_oid=doc["client_oid"], instrument_name=doc["instrument_name"],
            side=doc["side"], price=Decimal(doc["price"]), quantity=Decimal(doc["quantity"]),
        )
    except (ValueError, TypeError, ArithmeticError):
        raise OrderJournalError("Stored synthetic order intent is invalid.") from None
    if doc != _order_document(intent) or intent.client_oid != oid or intent.canonical_sha256() != intent_hash:
        raise OrderJournalError("Stored synthetic order intent does not match its fingerprint.")
    return doc


def _event_digest(oid: str, seq: int, kind: str, at: str, previous: str, payload_hash: str) -> str:
    return _hash(_json({
        "client_oid": oid, "seq": seq, "kind": kind, "at": at,
        "previous": previous, "payload_sha256": payload_hash,
    }))


class OfflineOrderJournal:
    """Safe rehearsal only: all writes are local, synthetic and permanently disarmed."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connection() as db:
            schema = db.execute("PRAGMA user_version").fetchone()[0]
            if schema not in (0, 1):
                raise OrderJournalError("Unrecognized offline order journal schema.")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS offline_order_intents (
                    id INTEGER PRIMARY KEY,
                    client_oid TEXT NOT NULL UNIQUE,
                    intent_json TEXT NOT NULL,
                    intent_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    authority_status TEXT NOT NULL
                        CHECK(authority_status = 'SYNTHETIC_ORDER_JOURNAL_ONLY'),
                    event_count INTEGER NOT NULL DEFAULT 0 CHECK(event_count >= 0),
                    event_head_sha256 TEXT NOT NULL DEFAULT
                        '0000000000000000000000000000000000000000000000000000000000000000'
                );
                CREATE TABLE IF NOT EXISTS offline_order_events (
                    id INTEGER PRIMARY KEY,
                    intent_id INTEGER NOT NULL REFERENCES offline_order_intents(id),
                    seq INTEGER NOT NULL CHECK(seq >= 1),
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    previous_sha256 TEXT NOT NULL,
                    event_sha256 TEXT NOT NULL,
                    UNIQUE(intent_id,seq)
                );
                CREATE TRIGGER IF NOT EXISTS offline_intent_immutable
                BEFORE UPDATE OF client_oid,intent_json,intent_sha256,created_at,authority_status
                ON offline_order_intents BEGIN
                    SELECT RAISE(ABORT,'offline intent immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS offline_intent_no_delete
                BEFORE DELETE ON offline_order_intents BEGIN
                    SELECT RAISE(ABORT,'offline intent immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS offline_events_no_update
                BEFORE UPDATE ON offline_order_events BEGIN
                    SELECT RAISE(ABORT,'offline order events immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS offline_events_no_delete
                BEFORE DELETE ON offline_order_events BEGIN
                    SELECT RAISE(ABORT,'offline order events immutable');
                END;
            """)
            db.execute("PRAGMA user_version=1")
        # Existing rows are checked on every audit/read. Never mutate their
        # state silently at startup: a prior reservation may have reached a
        # provider, and lack of a response is not absence of an order.

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

    @contextmanager
    def _write(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            else:
                db.execute("COMMIT")

    def _time(self) -> str:
        return _utc(self._clock()).isoformat()

    def _row(self, db: sqlite3.Connection, oid: str):
        row = db.execute("SELECT * FROM offline_order_intents WHERE client_oid=?", (oid,)).fetchone()
        if row is None:
            raise OrderJournalError("Synthetic client_oid not found; nothing may be retried.")
        return row

    @staticmethod
    def _append(db: sqlite3.Connection, row, kind: str, payload: dict, at: str):
        if kind not in _EVENT_TYPES:
            raise OrderJournalError("Unknown synthetic journal event.")
        if type(row["event_count"]) is not int or not 0 <= row["event_count"] < _MAX_EVENTS:
            raise OrderJournalError("Synthetic journal event quota reached or count corrupted.")
        prev = row["event_head_sha256"]
        if not isinstance(prev, str) or _SHA256.fullmatch(prev) is None:
            raise OrderJournalError("Synthetic journal head fingerprint is corrupt.")
        seq = row["event_count"] + 1
        payload_json = _json(payload)
        payload_hash = _hash(payload_json)
        digest = _event_digest(row["client_oid"], seq, kind, at, prev, payload_hash)
        db.execute("""INSERT INTO offline_order_events
            (intent_id,seq,event_type,created_at,payload_json,payload_sha256,previous_sha256,event_sha256)
            VALUES(?,?,?,?,?,?,?,?)""",
            (row["id"], seq, kind, at, payload_json, payload_hash, prev, digest))
        db.execute("""UPDATE offline_order_intents SET event_count=?,event_head_sha256=? WHERE id=?""",
                   (seq, digest, row["id"]))

    def record_intent(self, intent: CryptoComSpotLimitOrder) -> dict:
        """Persist immutable, synthetic intent. Never consume approval or send."""
        document = _order_document(intent)
        digest = intent.canonical_sha256()
        stamp = self._time()
        with self._write() as db:
            # A duplicate is rejected even if all values match. The caller
            # must inspect history; do not silently treat duplicates as safe.
            try:
                cursor = db.execute("""INSERT INTO offline_order_intents
                    (client_oid,intent_json,intent_sha256,created_at,authority_status)
                    VALUES(?,?,?,?,?)""",
                    (intent.client_oid, _json(document), digest, stamp, _AUTHORITY))
            except sqlite3.IntegrityError:
                raise OrderJournalError("client_oid already exists in the durable journal.") from None
            row = db.execute("SELECT * FROM offline_order_intents WHERE id=?", (cursor.lastrowid,)).fetchone()
            self._append(db, row, "INTENT_RECORDED", {}, stamp)
        return self.get(intent.client_oid)

    def _checked_entry(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict:
        if (row["authority_status"] != _AUTHORITY or
                type(row["event_count"]) is not int or row["event_count"] < 1 or
                row["event_count"] > _MAX_EVENTS):
            raise OrderJournalError("Untrusted offline order intent authority/count.")
        try:
            document = json.loads(row["intent_json"])
        except (ValueError, TypeError):
            raise OrderJournalError("Corrupted synthetic order intent JSON.") from None
        _verify_document(document, row["intent_sha256"], row["client_oid"])
        try:
            parsed = datetime.fromisoformat(row["created_at"])
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("naive")
        except (ValueError, TypeError, AttributeError):
            raise OrderJournalError("Corrupted synthetic intent timestamp.") from None
        events = db.execute("""SELECT * FROM offline_order_events
            WHERE intent_id=? ORDER BY seq ASC""", (row["id"],)).fetchall()
        if len(events) != row["event_count"]:
            raise OrderJournalError("Synthetic journal contains missing or extra events.")
        previous = _GENESIS
        replay: list[dict] = []
        risk_reservation = None
        for position, entry in enumerate(events, 1):
            payload_json = entry["payload_json"]
            if (entry["seq"] != position or entry["previous_sha256"] != previous
                    or entry["event_type"] not in _EVENT_TYPES
                    or _hash(payload_json) != entry["payload_sha256"]
                    or _event_digest(row["client_oid"], position, entry["event_type"],
                                     entry["created_at"], previous, entry["payload_sha256"])
                       != entry["event_sha256"]):
                raise OrderJournalError("Synthetic journal hash chain or sequence is corrupt.")
            try:
                payload = json.loads(payload_json)
                event_time = datetime.fromisoformat(entry["created_at"])
                if event_time.tzinfo is None or event_time.utcoffset() is None:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise OrderJournalError("Synthetic journal event payload/time is corrupt.") from None
            if not isinstance(payload, dict):
                raise OrderJournalError("Synthetic journal event payload is invalid.")
            kind = entry["event_type"]
            if position == 1 and (kind != "INTENT_RECORDED" or payload != {}):
                raise OrderJournalError("Synthetic journal has no valid genesis event.")
            if position > 1 and kind == "INTENT_RECORDED":
                raise OrderJournalError("Synthetic journal has duplicate genesis.")
            if kind == "SIMULATED_RISK_RESERVED":
                required = {
                    "approval_id", "approval_sha256", "consumption_sha256",
                    "quote_currency", "reservation_amount", "transaction_sha256",
                }
                if position != 2 or risk_reservation is not None or set(payload) != required:
                    raise OrderJournalError("Synthetic Risk reservation event is duplicated or misplaced.")
                if (not isinstance(payload["approval_id"], str)
                        or not isinstance(payload["quote_currency"], str)
                        or re.fullmatch(r"[A-Z0-9]{2,12}", payload["quote_currency"]) is None
                        or not isinstance(payload["reservation_amount"], str)
                        or re.fullmatch(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,8})?", payload["reservation_amount"]) is None
                        or any(not isinstance(payload[key], str) or _SHA256.fullmatch(payload[key]) is None
                               for key in ("approval_sha256", "consumption_sha256", "transaction_sha256"))):
                    raise OrderJournalError("Synthetic Risk reservation event contains invalid values.")
                material = {
                    "client_oid": row["client_oid"], "intent_sha256": row["intent_sha256"],
                    "approval_id": payload["approval_id"], "approval_sha256": payload["approval_sha256"],
                    "consumption_sha256": payload["consumption_sha256"],
                    "quote_currency": payload["quote_currency"],
                    "reservation_amount": payload["reservation_amount"],
                }
                if _hash(_json(material)) != payload["transaction_sha256"]:
                    raise OrderJournalError("Synthetic Risk reservation transaction fingerprint is invalid.")
                risk_reservation = payload
            step = {
                "SIMULATED_ATTEMPT_RESERVED": "ATTEMPT",
                "RESPONSE_UNCERTAIN": "RESPONSE_UNCERTAIN",
                "ACK_OBSERVED": "ACK",
                "READBACK_OBSERVED": "READBACK",
                "READBACK_FAILED": "READBACK_FAILED",
            }.get(kind)
            expected = ({"order_id", "status", "cumulative_quantity"} if kind == "READBACK_OBSERVED"
                        else {"order_id"} if kind == "ACK_OBSERVED"
                        else {"approval_id", "approval_sha256", "consumption_sha256",
                              "quote_currency", "reservation_amount", "transaction_sha256"}
                        if kind == "SIMULATED_RISK_RESERVED" else set())
            if set(payload) != expected:
                raise OrderJournalError("Synthetic journal event contract differs from its type.")
            if step is not None:
                replay.append({
                    "step": step, "client_oid": row["client_oid"],
                    "intent_sha256": row["intent_sha256"], "observed_at": entry["created_at"],
                    "order_id": payload.get("order_id"),
                    "status": payload.get("status"),
                    "cumulative_quantity": payload.get("cumulative_quantity"),
                })
            previous = entry["event_sha256"]
        if previous != row["event_head_sha256"]:
            raise OrderJournalError("Synthetic journal head does not match its event chain.")
        try:
            audit = replay_offline_order_evidence(
                intent={"client_oid": row["client_oid"], "intent_sha256": row["intent_sha256"],
                        "quantity": document["quantity"]}, events=replay)
        except (ValueError, TypeError, ArithmeticError):
            raise OrderJournalError("Synthetic journal event semantics are inconsistent.") from None
        has_attempt = any(e["step"] == "ATTEMPT" for e in replay)
        # An observed terminal state does not prove balanced fills, account
        # authorization or that a new order may safely be submitted.
        return {
            "client_oid": row["client_oid"], "intent_sha256": row["intent_sha256"],
            "instrument_name": document["instrument_name"], "side": document["side"],
            "created_at": row["created_at"], "event_count": row["event_count"],
            "head_sha256": previous, "journal_status": audit["status"],
            "observed_status": audit["last_exchange_status_observed"],
            "needs_manual_reconciliation": has_attempt,
            "uncertain_after_restart": audit["needs_manual_readback"],
            "attempt_count": audit["attempt_count"],
            "reason_codes": audit["reasons"],
            "risk_reservation": risk_reservation,
            "authority_status": _AUTHORITY, "simulation_only": True,
            "can_submit": False, "can_retry_automatically": False,
            "usable_for_risk": False, "usable_for_execution": False,
            "orders_enabled": False,
        }

    def get(self, oid: str) -> dict:
        with self._connection() as db:
            return self._checked_entry(db, self._row(db, oid))

    def _append_checked(self, oid: str, kind: str, payload: dict) -> dict:
        with self._write() as db:
            row = self._row(db, oid)
            checked = self._checked_entry(db, row)
            last = db.execute("""SELECT event_type FROM offline_order_events
                WHERE intent_id=? ORDER BY seq DESC LIMIT 1""", (row["id"],)).fetchone()[0]
            if kind == "SIMULATED_ATTEMPT_RESERVED":
                if last != "INTENT_RECORDED" or checked["attempt_count"]:
                    raise OrderJournalError("Synthetic attempt already reserved or not in draft state.")
            elif kind in {"RESPONSE_UNCERTAIN", "READBACK_FAILED", "ACK_OBSERVED", "READBACK_OBSERVED"}:
                if checked["attempt_count"] != 1:
                    raise OrderJournalError("No durable synthetic attempt to reconcile.")
                if kind == "ACK_OBSERVED" and checked["observed_status"] is not None:
                    raise OrderJournalError("Synthetic readback exists; late ACK requires manual review.")
                if kind == "ACK_OBSERVED" and last == "ACK_OBSERVED":
                    raise OrderJournalError("Duplicate synthetic acknowledgement.")
            else:
                raise OrderJournalError("Synthetic event transition is unsupported.")
            stamp = self._time()
            self._append(db, row, kind, payload, stamp)
            # Validate the entire post-append chain *inside the transaction*.
            # Invalid events must roll back, never poison a committed journal.
            new_row = self._row(db, oid)
            self._checked_entry(db, new_row)
        return self.get(oid)

    def reserve_simulated_attempt(self, oid: str) -> dict:
        """Atomic durable claim for OFFLINE rehearsal. Does not POST anywhere."""
        return self._append_checked(oid, "SIMULATED_ATTEMPT_RESERVED", {})

    def record_uncertain_response(self, oid: str) -> dict:
        """Mark a synthetic timeout; never release client_oid for retry."""
        return self._append_checked(oid, "RESPONSE_UNCERTAIN", {})

    def record_simulated_ack(self, oid: str, order_id: str) -> dict:
        if not isinstance(order_id, str) or _ORDER_ID.fullmatch(order_id) is None or int(order_id) <= 0:
            raise OrderJournalError("Synthetic order id must be a positive decimal identity.")
        return self._append_checked(oid, "ACK_OBSERVED", {"order_id": order_id})

    def record_simulated_readback(self, oid: str, *, order_id: str,
                                  status: str, cumulative_quantity: str) -> dict:
        if (not isinstance(order_id, str) or _ORDER_ID.fullmatch(order_id) is None
                or int(order_id) <= 0 or status not in _STATES
                or not isinstance(cumulative_quantity, str) or
                len(cumulative_quantity) > 80):
            raise OrderJournalError("Synthetic readback has invalid identity/status/quantity.")
        # Semantics and quantities are also checked during full replay, before
        # transaction commit; contradictory evidence remains visible as blocked.
        return self._append_checked(oid, "READBACK_OBSERVED", {
            "order_id": order_id, "status": status,
            "cumulative_quantity": cumulative_quantity,
        })

    def record_simulated_readback_failure(self, oid: str) -> dict:
        return self._append_checked(oid, "READBACK_FAILED", {})

    def audit(self, *, limit: int = 20) -> dict:
        """Read/verify ALL entries (not just visible rows); no provider lookup."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OrderJournalError("Invalid journal display limit.")
        with self._connection() as db:
            rows = db.execute("SELECT * FROM offline_order_intents ORDER BY id DESC").fetchall()
            if len(rows) > 10000:
                raise OrderJournalError("Unbounded synthetic journal review requires offline inspection.")
            entries = [self._checked_entry(db, row) for row in rows]
        report = {
            "status": "BLOCKED_SIMULATION_ONLY",
            "authority_status": _AUTHORITY, "simulation_only": True,
            "total_intents": len(entries),
            "manual_reconciliation_count": sum(1 for entry in entries if entry["needs_manual_reconciliation"]),
            "uncertain_count": sum(1 for entry in entries if entry["uncertain_after_restart"]),
            "entries": entries[:limit],
            "all_entries_verified": True,
            "recovery_mode": "MANUAL_REVIEW_ONLY",
            "provider_history_authenticated": False,
            "persistent_client_oid_uniqueness": True,
            "linked_to_live_order_adapter": False,
            "usable_for_risk": False, "usable_for_execution": False,
            "can_submit": False, "can_retry_automatically": False, "orders_enabled": False,
        }
        # Informational digest, not a MAC/signature or trusted external anchor.
        report["audit_sha256"] = _hash(_json({"records": entries, "summary": report}))
        return report


__all__ = ["OfflineOrderJournal", "OrderJournalError"]
