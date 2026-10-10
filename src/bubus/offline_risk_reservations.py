"""OFFLINE-ONLY atomic capital reservations and single-use Risk approval rehearsal.

No real balance, user authorization, RiskPolicy, broker, network, or order
transport is accepted. This cannot implement OrderAuthorizationGate: it never
returns OrderAuthorization or an order-execution permission.

One SQLite transaction atomically consumes an explicitly recorded synthetic
approval and reserves its exact BUY exposure, including an explicit fee buffer.
Reserved funds are never auto-released; this deliberately fails closed.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP, localcontext
from pathlib import Path
from typing import Callable

from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder

_AUTH = "SYNTHETIC_RISK_RESERVATION_ONLY"
_GENESIS = "0" * 64
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CURRENCY = re.compile(r"[A-Z0-9]{2,12}\Z")
_AMOUNT = re.compile(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,8})?\Z")
_BUFFER = re.compile(r"(?:0(?:\.[0-9]{1,8})?|1(?:\.0{1,8})?)\Z")
_MONEY_UNIT = Decimal("0.00000001")
_MAX_ROWS = 10000


class OfflineRiskError(ValueError):
    """An untrusted, duplicated or incomplete synthetic approval is refused."""


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clock(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OfflineRiskError("A timezone-aware simulation clock is required.")
    return value.astimezone(timezone.utc)


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 48:
        raise OfflineRiskError("Untrusted simulation timestamp.")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise OfflineRiskError("Untrusted simulation timestamp.") from None
    return _clock(stamp)


def _id(value: object, label: str) -> str:
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise OfflineRiskError(f"{label} must be a canonical UUID.") from None
    if canonical != value:
        raise OfflineRiskError(f"{label} must be a canonical UUID.")
    return canonical


def _money(value: object, label: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or _AMOUNT.fullmatch(value) is None:
        raise OfflineRiskError(f"{label} must be a bounded explicit decimal string.")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise OfflineRiskError(f"{label} is not a decimal.") from None
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise OfflineRiskError(f"{label} must be finite and nonnegative.")
    return result


def _buffer(value: object) -> Decimal:
    if not isinstance(value, str) or _BUFFER.fullmatch(value) is None:
        raise OfflineRiskError("An explicit fractional fee cushion between 0 and 1 is required.")
    return Decimal(value)


def _currency(value: object) -> str:
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        raise OfflineRiskError("Simulation quote currency is invalid.")
    return value


def _notional(intent: CryptoComSpotLimitOrder, fee_buffer: Decimal) -> str:
    if not isinstance(intent, CryptoComSpotLimitOrder) or intent.side != "BUY":
        raise OfflineRiskError("Only synthetic BUY Spot reservations are modeled; SELL inventory is unknown.")
    with localcontext() as ctx:
        ctx.prec = 60
        amount = intent.price * intent.quantity * (Decimal(1) + fee_buffer)
        if not amount.is_finite() or amount <= 0 or amount >= Decimal("1e18"):
            raise OfflineRiskError("Synthetic total exposure is outside supported bounds.")
        rounded = amount.quantize(_MONEY_UNIT, rounding=ROUND_UP)
    if rounded <= 0 or rounded >= Decimal("1e18"):
        raise OfflineRiskError("Synthetic total exposure cannot be represented.")
    return format(rounded, "f")


def _intent_doc(intent: CryptoComSpotLimitOrder) -> dict:
    if not isinstance(intent, CryptoComSpotLimitOrder):
        raise OfflineRiskError("Validated Spot limit intent required.")
    return {
        "client_oid": intent.client_oid, "instrument_name": intent.instrument_name,
        "side": intent.side, "price": format(intent.price, "f"),
        "quantity": format(intent.quantity, "f"), "order_type": "LIMIT",
        "time_in_force": "FILL_OR_KILL",
    }


def _validate_intent(raw: object, sha: object) -> CryptoComSpotLimitOrder:
    if not isinstance(raw, dict) or set(raw) != {
        "client_oid", "instrument_name", "side", "price", "quantity",
        "order_type", "time_in_force",
    } or raw["order_type"] != "LIMIT" or raw["time_in_force"] != "FILL_OR_KILL":
        raise OfflineRiskError("Stored synthetic intent has invalid shape.")
    try:
        intent = CryptoComSpotLimitOrder(
            client_oid=raw["client_oid"], instrument_name=raw["instrument_name"],
            side=raw["side"], price=Decimal(raw["price"]), quantity=Decimal(raw["quantity"]),
        )
    except (ValueError, TypeError, ArithmeticError):
        raise OfflineRiskError("Stored synthetic order is invalid.") from None
    if not isinstance(sha, str) or _SHA.fullmatch(sha) is None or intent.canonical_sha256() != sha:
        raise OfflineRiskError("Synthetic intent hash mismatch.")
    if raw != _intent_doc(intent):
        raise OfflineRiskError("Synthetic intent canonical representation mismatch.")
    return intent


class OfflineRiskReservations:
    """Isolated simulation store. This object is never an execution gate."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise OfflineRiskError("Unknown offline risk schema version.")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS synthetic_risk_budget(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    created_at TEXT NOT NULL,
                    quote_currency TEXT NOT NULL,
                    capital_limit TEXT NOT NULL,
                    max_order_notional TEXT NOT NULL,
                    max_daily_loss TEXT NOT NULL,
                    observed_daily_loss TEXT NOT NULL,
                    fee_cushion TEXT NOT NULL,
                    source TEXT NOT NULL
                        CHECK(source='EXPLICIT_SYNTHETIC_TEST_FIXTURE'),
                    config_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS synthetic_risk_approvals(
                    id INTEGER PRIMARY KEY,
                    approval_id TEXT UNIQUE NOT NULL,
                    client_oid TEXT UNIQUE NOT NULL,
                    intent_json TEXT NOT NULL,
                    intent_sha256 TEXT NOT NULL,
                    quote_currency TEXT NOT NULL,
                    reservation_amount TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approver_label TEXT NOT NULL,
                    budget_sha256 TEXT NOT NULL,
                    approval_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('PENDING','RESERVED')),
                    consumed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS synthetic_risk_consumptions(
                    id INTEGER PRIMARY KEY,
                    approval_id TEXT NOT NULL UNIQUE
                        REFERENCES synthetic_risk_approvals(approval_id),
                    consumed_at TEXT NOT NULL,
                    consumption_sha256 TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS synthetic_consumption_no_update
                BEFORE UPDATE ON synthetic_risk_consumptions BEGIN
                    SELECT RAISE(ABORT,'synthetic approval consumption immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS synthetic_consumption_no_delete
                BEFORE DELETE ON synthetic_risk_consumptions BEGIN
                    SELECT RAISE(ABORT,'synthetic approval consumption immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS synthetic_budget_immutable
                BEFORE UPDATE ON synthetic_risk_budget BEGIN
                    SELECT RAISE(ABORT,'simulation budget immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS synthetic_budget_no_delete
                BEFORE DELETE ON synthetic_risk_budget BEGIN
                    SELECT RAISE(ABORT,'simulation budget immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS synthetic_approval_no_delete
                BEFORE DELETE ON synthetic_risk_approvals BEGIN
                    SELECT RAISE(ABORT,'simulation approval immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS synthetic_approval_no_identity_update
                BEFORE UPDATE OF approval_id,client_oid,intent_json,intent_sha256,
                    quote_currency,reservation_amount,approved_at,expires_at,
                    approver_label,budget_sha256,approval_sha256
                ON synthetic_risk_approvals BEGIN
                    SELECT RAISE(ABORT,'simulation approval immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS synthetic_approval_no_unconsume
                BEFORE UPDATE ON synthetic_risk_approvals
                WHEN OLD.state='RESERVED' AND NEW.state!='RESERVED' BEGIN
                    SELECT RAISE(ABORT,'simulation reservation cannot be released');
                END;
            """)
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def _connect(self):
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
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            else:
                db.execute("COMMIT")

    def _now(self) -> datetime:
        return _clock(self._clock())

    def set_synthetic_budget(self, *, quote_currency: str, capital_limit: str,
                             max_order_notional: str, max_daily_loss: str,
                             observed_daily_loss: str, fee_cushion: str) -> dict:
        """One explicit non-authoritative scenario, immutable after first write."""
        currency = _currency(quote_currency)
        capital = _money(capital_limit, "capital_limit", positive=True)
        order = _money(max_order_notional, "max_order_notional", positive=True)
        loss_cap = _money(max_daily_loss, "max_daily_loss", positive=True)
        daily_loss = _money(observed_daily_loss, "observed_daily_loss")
        cushion = _buffer(fee_cushion)
        if order > capital or loss_cap > capital:
            raise OfflineRiskError("Explicit synthetic order/loss caps exceed synthetic capital.")
        config = {
            "quote_currency": currency, "capital_limit": capital_limit,
            "max_order_notional": max_order_notional,
            "max_daily_loss": max_daily_loss, "observed_daily_loss": observed_daily_loss,
            "fee_cushion": fee_cushion, "source": "EXPLICIT_SYNTHETIC_TEST_FIXTURE",
        }
        stamp = self._now().isoformat()
        with self._write() as db:
            if db.execute("SELECT 1 FROM synthetic_risk_budget").fetchone():
                raise OfflineRiskError("Simulation budget already exists and cannot be rewritten.")
            db.execute("""INSERT INTO synthetic_risk_budget
                (id,created_at,quote_currency,capital_limit,max_order_notional,max_daily_loss,
                observed_daily_loss,fee_cushion,source,config_sha256)
                VALUES(1,?,?,?,?,?,?,?,?,?)""",
                (stamp, currency, capital_limit, max_order_notional, max_daily_loss,
                 observed_daily_loss, fee_cushion, config["source"], _sha(_dump(config))))
        return self.audit()

    def _budget(self, db):
        row = db.execute("SELECT * FROM synthetic_risk_budget").fetchone()
        if row is None:
            return None
        if row["id"] != 1 or row["source"] != "EXPLICIT_SYNTHETIC_TEST_FIXTURE":
            raise OfflineRiskError("Untrusted simulation source.")
        config = {
            "quote_currency": row["quote_currency"], "capital_limit": row["capital_limit"],
            "max_order_notional": row["max_order_notional"],
            "max_daily_loss": row["max_daily_loss"],
            "observed_daily_loss": row["observed_daily_loss"],
            "fee_cushion": row["fee_cushion"], "source": row["source"],
        }
        _currency(config["quote_currency"])
        capital = _money(config["capital_limit"], "capital_limit", positive=True)
        limit = _money(config["max_order_notional"], "max_order_notional", positive=True)
        loss = _money(config["max_daily_loss"], "max_daily_loss", positive=True)
        daily = _money(config["observed_daily_loss"], "observed_daily_loss")
        cushion = _buffer(config["fee_cushion"])
        if limit > capital or loss > capital or _sha(_dump(config)) != row["config_sha256"]:
            raise OfflineRiskError("Synthetic capital limits have been tampered with.")
        _parse_time(row["created_at"])
        return row, capital, limit, loss, daily, cushion

    def _approvals(self, db, budget):
        records = db.execute("SELECT * FROM synthetic_risk_approvals ORDER BY id").fetchall()
        if len(records) > _MAX_ROWS:
            raise OfflineRiskError("Too many synthetic approvals for this audit.")
        unique_oid = set()
        unique_approval = set()
        for row in records:
            if row["approval_id"] in unique_approval or row["client_oid"] in unique_oid:
                raise OfflineRiskError("Duplicate synthetic order approval detected.")
            unique_oid.add(row["client_oid"])
            unique_approval.add(row["approval_id"])
            _id(row["approval_id"], "approval_id")
            _id(row["client_oid"], "client_oid")
            if (row["state"] not in {"PENDING", "RESERVED"} or
                    not isinstance(row["approver_label"], str) or
                    not 1 <= len(row["approver_label"]) <= 64 or
                    not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row["approver_label"])):
                raise OfflineRiskError("Invalid simulation-only human approval provenance.")
            stamp = _parse_time(row["approved_at"])
            expiry = _parse_time(row["expires_at"])
            if not stamp < expiry <= stamp + timedelta(minutes=30):
                raise OfflineRiskError("Synthetic human approval expiry is invalid.")
            if (row["state"] == "PENDING" and row["consumed_at"] is not None or
                    row["state"] == "RESERVED" and row["consumed_at"] is None):
                raise OfflineRiskError("Synthetic approval state is inconsistent.")
            if row["consumed_at"] is not None and _parse_time(row["consumed_at"]) < stamp:
                raise OfflineRiskError("Synthetic approval consumed before it was issued.")
            try:
                doc = json.loads(row["intent_json"])
            except (ValueError, TypeError):
                raise OfflineRiskError("Stored synthetic approval intent JSON invalid.") from None
            intent = _validate_intent(doc, row["intent_sha256"])
            if intent.client_oid != row["client_oid"] or row["quote_currency"] != budget[0]["quote_currency"]:
                raise OfflineRiskError("Synthetic approval intent/currency mismatch.")
            if intent.instrument_name.split("_")[-1] != row["quote_currency"]:
                raise OfflineRiskError("Synthetic asset quote currency differs from simulation budget.")
            notional = _notional(intent, budget[5])
            if row["reservation_amount"] != notional or Decimal(notional) > budget[2]:
                raise OfflineRiskError("Synthetic amount/fee cap inconsistent.")
            if row["budget_sha256"] != budget[0]["config_sha256"]:
                raise OfflineRiskError("Synthetic approval budget revision mismatch.")
            material = {
                "approval_id": row["approval_id"], "client_oid": row["client_oid"],
                "intent_sha256": row["intent_sha256"], "reservation_amount": notional,
                "quote_currency": row["quote_currency"], "approved_at": row["approved_at"],
                "expires_at": row["expires_at"], "approver_label": row["approver_label"],
                "budget_sha256": row["budget_sha256"],
            }
            if row["approval_sha256"] != _sha(_dump(material)):
                raise OfflineRiskError("Synthetic human approval fingerprint invalid.")
            receipt = db.execute(
                "SELECT * FROM synthetic_risk_consumptions WHERE approval_id=?",
                (row["approval_id"],),
            ).fetchone()
            if row["state"] == "RESERVED":
                if receipt is None or receipt["consumed_at"] != row["consumed_at"]:
                    raise OfflineRiskError("Synthetic capital reservation has no durable consumption receipt.")
                recorded = {
                    "approval_id": row["approval_id"], "approval_sha256": row["approval_sha256"],
                    "intent_sha256": row["intent_sha256"], "amount": notional,
                    "consumed_at": row["consumed_at"],
                }
                if receipt["consumption_sha256"] != _sha(_dump(recorded)):
                    raise OfflineRiskError("Synthetic one-use receipt hash is invalid.")
            elif receipt is not None:
                raise OfflineRiskError("Pending simulation approval cannot have a consumption receipt.")
            yield row
        # Entire portfolio aggregation is checked by audit()/reserve using
        # a separate sum after validating every approval in this iterator.

    def _checked(self, db):
        budget = self._budget(db)
        if budget is None:
            if (db.execute("SELECT COUNT(*) FROM synthetic_risk_approvals").fetchone()[0]
                    or db.execute("SELECT COUNT(*) FROM synthetic_risk_consumptions").fetchone()[0]):
                raise OfflineRiskError("Synthetic approvals or consumption receipts exist without a scenario budget.")
            return None, [], Decimal(0)
        approvals = list(self._approvals(db, budget))
        receipt_count = db.execute(
            "SELECT COUNT(*) FROM synthetic_risk_consumptions"
        ).fetchone()[0]
        if receipt_count != sum(row["state"] == "RESERVED" for row in approvals):
            raise OfflineRiskError("Synthetic consumption receipt count is inconsistent.")
        reserved = sum(
            (Decimal(row["reservation_amount"]) for row in approvals if row["state"] == "RESERVED"),
            Decimal(0),
        )
        if reserved > budget[1]:
            raise OfflineRiskError("Total simulated reservations exceed the synthetic capital limit.")
        return budget, approvals, reserved

    def record_synthetic_human_approval(self, intent: CryptoComSpotLimitOrder, *,
                                        approval_id: str, approver_label: str,
                                        expires_at: datetime) -> dict:
        """Stage a fictional human approval. NO human authentication is inferred."""
        _id(approval_id, "approval_id")
        if not isinstance(approver_label, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", approver_label) is None:
            raise OfflineRiskError("A synthetic approver label is required.")
        document = _intent_doc(intent)
        stamp = self._now()
        expiry = _clock(expires_at)
        if not stamp < expiry <= stamp + timedelta(minutes=30):
            raise OfflineRiskError("Simulation approval must expire within thirty minutes.")
        with self._write() as db:
            budget, _, _ = self._checked(db)
            if budget is None:
                raise OfflineRiskError("Explicit simulation capital limits required.")
            if budget[4] >= budget[3]:
                raise OfflineRiskError("Synthetic daily loss threshold already reached.")
            if intent.instrument_name.split("_")[-1] != budget[0]["quote_currency"]:
                raise OfflineRiskError("Synthetic FX conversion is not supported.")
            amount = _notional(intent, budget[5])
            if Decimal(amount) > budget[2]:
                raise OfflineRiskError("Simulated exposure exceeds per-order cap including fees.")
            material = {
                "approval_id": approval_id, "client_oid": intent.client_oid,
                "intent_sha256": intent.canonical_sha256(),
                "reservation_amount": amount, "quote_currency": budget[0]["quote_currency"],
                "approved_at": stamp.isoformat(), "expires_at": expiry.isoformat(),
                "approver_label": approver_label, "budget_sha256": budget[0]["config_sha256"],
            }
            try:
                db.execute("""INSERT INTO synthetic_risk_approvals
                    (approval_id,client_oid,intent_json,intent_sha256,quote_currency,
                    reservation_amount,approved_at,expires_at,approver_label,budget_sha256,
                    approval_sha256,state,consumed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,'PENDING',NULL)""",
                    (approval_id, intent.client_oid, _dump(document), intent.canonical_sha256(),
                     budget[0]["quote_currency"], amount, material["approved_at"],
                     material["expires_at"], approver_label, budget[0]["config_sha256"],
                     _sha(_dump(material))))
            except sqlite3.IntegrityError:
                raise OfflineRiskError("Synthetic approval/client_oid already exists.") from None
        return self.audit()

    def consume_and_reserve_simulated(self, *, approval_id: str,
                                      intent: CryptoComSpotLimitOrder) -> dict:
        """Consume approval ONCE and reserve money in one BEGIN IMMEDIATE tx.

        Never returns OrderAuthorization. Reboots and competing connections
        cannot re-consume an approval or spend the same synthetic balance.
        """
        _id(approval_id, "approval_id")
        if not isinstance(intent, CryptoComSpotLimitOrder):
            raise OfflineRiskError("Validated synthetic Spot intent required.")
        with self._write() as db:
            budget, approvals, reserved = self._checked(db)
            if budget is None:
                raise OfflineRiskError("No explicit simulation budget exists.")
            row = next((r for r in approvals if r["approval_id"] == approval_id), None)
            if row is None:
                raise OfflineRiskError("Synthetic approval not found.")
            if row["state"] != "PENDING" or row["consumed_at"] is not None:
                raise OfflineRiskError("Synthetic approval already consumed; replay refused.")
            if (row["client_oid"] != intent.client_oid or
                    row["intent_sha256"] != intent.canonical_sha256() or
                    json.loads(row["intent_json"]) != _intent_doc(intent)):
                raise OfflineRiskError("Intent changed after synthetic approval.")
            now = self._now()
            approved_at, expires = _parse_time(row["approved_at"]), _parse_time(row["expires_at"])
            if not approved_at <= now < expires:
                raise OfflineRiskError("Synthetic human approval expired or clock regressed.")
            if budget[4] >= budget[3]:
                raise OfflineRiskError("Synthetic daily-loss budget exceeded.")
            amount = Decimal(row["reservation_amount"])
            if amount > budget[2] or amount > budget[1] - reserved:
                raise OfflineRiskError("Insufficient explicit synthetic available capital.")
            cursor = db.execute("""UPDATE synthetic_risk_approvals
                SET state='RESERVED', consumed_at=?
                WHERE id=? AND state='PENDING' AND consumed_at IS NULL""",
                (now.isoformat(), row["id"]))
            if cursor.rowcount != 1:
                raise OfflineRiskError("Synthetic approval already consumed by another writer.")
            receipt_material = {
                "approval_id": approval_id, "approval_sha256": row["approval_sha256"],
                "intent_sha256": row["intent_sha256"],
                "amount": row["reservation_amount"],
                "consumed_at": now.isoformat(),
            }
            db.execute("""INSERT INTO synthetic_risk_consumptions
                (approval_id,consumed_at,consumption_sha256) VALUES(?,?,?)""",
                (approval_id, now.isoformat(), _sha(_dump(receipt_material))))
            # Re-read all rows before COMMIT to fail closed on inconsistencies.
            _, _, total_after = self._checked(db)
            if total_after != reserved + amount:
                raise OfflineRiskError("Synthetic atomic reservation ledger inconsistent.")
        return self.audit()

    def audit(self, *, limit: int = 20) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OfflineRiskError("Invalid synthetic report limit.")
        with self._connect() as db:
            # Freeze a consistent multi-table snapshot for this read-only audit.
            db.execute("BEGIN")
            try:
                budget, approvals, reserved = self._checked(db)
            finally:
                db.execute("ROLLBACK")
        pending_count = sum(row["state"] == "PENDING" for row in approvals)
        consumed_count = sum(row["state"] == "RESERVED" for row in approvals)
        remaining = None if budget is None else budget[1] - reserved
        entries = [{
            "approval_id": row["approval_id"], "client_oid": row["client_oid"],
            "intent_sha256": row["intent_sha256"],
            "amount": row["reservation_amount"],
            "quote_currency": row["quote_currency"],
            "state": row["state"],
            "consumed_at": row["consumed_at"],
            "expires_at": row["expires_at"],
            "human_approval_authenticated": False,
            "can_submit": False, "orders_enabled": False,
        } for row in reversed(approvals[-limit:])]
        report = {
            "status": "BLOCKED_SIMULATION_ONLY",
            "authority_status": _AUTH, "simulation_only": True,
            "explicit_budget_present": budget is not None,
            "source": None if budget is None else "EXPLICIT_SYNTHETIC_TEST_FIXTURE",
            "quote_currency": None if budget is None else budget[0]["quote_currency"],
            "capital_limit": None if budget is None else budget[0]["capital_limit"],
            "max_order_notional": None if budget is None else budget[0]["max_order_notional"],
            "max_daily_loss": None if budget is None else budget[0]["max_daily_loss"],
            "observed_daily_loss": None if budget is None else budget[0]["observed_daily_loss"],
            "fee_cushion": None if budget is None else budget[0]["fee_cushion"],
            "reserved_capital": None if budget is None else format(reserved, "f"),
            "remaining_synthetic_capital": None if remaining is None else format(remaining, "f"),
            "pending_approvals": pending_count, "consumed_approvals": consumed_count,
            "total_approvals": len(approvals), "entries": entries,
            "all_entries_verified": True, "reserve_and_consume_atomic": True,
            "release_after_uncertain_execution_allowed": False,
            "approval_is_real_human_authentication": False,
            "connected_to_order_adapter": False,
            "cross_journal_atomicity": False,
            "usable_for_risk": False, "usable_for_execution": False,
            "can_submit": False, "can_retry_automatically": False,
            "orders_enabled": False,
        }
        # SHA is only an informative internal checksum, not an external MAC.
        report["audit_sha256"] = _sha(_dump(report))
        return report


__all__ = ["OfflineRiskReservations", "OfflineRiskError"]
