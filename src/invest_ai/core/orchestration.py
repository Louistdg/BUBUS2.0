"""Durable, fail-closed orchestration state; this module never executes trades."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from invest_ai.core.clock import iso_utc, utc_now
from invest_ai.core.errors import JobStateError
from invest_ai.core.ids import new_id
from invest_ai.policy import PolicyDecision
from invest_ai.risk.models import RiskDecision, TradeProposal


class WorkflowState(StrEnum):
    PROPOSED = "PROPOSED"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_REVIEW_REQUIRED = "RISK_REVIEW_REQUIRED"
    RISK_ACCEPTED = "RISK_ACCEPTED"
    POLICY_REJECTED = "POLICY_REJECTED"
    POLICY_ACCEPTED = "POLICY_ACCEPTED"
    PAPER_PENDING = "PAPER_PENDING"
    PAPER_COMPLETED = "PAPER_COMPLETED"
    FAILED = "FAILED"


class OrchestrationConflict(JobStateError):
    """An idempotency key or immutable decision was replayed with new content."""


class KillSwitchActive(JobStateError):
    pass


class TrustedRiskUnavailable(JobStateError):
    """A required operational Risk source is absent or lacks verified authority."""


@dataclass(frozen=True, slots=True)
class RiskEvaluationReceipt:
    receipt_id: str
    proposal_id: str
    proposal_digest: str
    policy_revision_id: str
    policy_digest: str
    portfolio_snapshot_id: str
    portfolio_revision: int
    portfolio_digest: str
    market_snapshot_id: str
    market_revision: int
    market_digest: str
    decision: RiskDecision
    control_epoch: int
    recorded_at: str


@dataclass(frozen=True, slots=True)
class OrchestrationControlState:
    """Persisted control facts read atomically from the runtime store."""

    halted: bool
    control_epoch: int
    changed_at: str
    actor: str
    reason: str


_SCHEMA_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS orchestration_meta(version INTEGER NOT NULL)",
    "INSERT INTO orchestration_meta(version) SELECT 4 WHERE NOT EXISTS (SELECT 1 FROM orchestration_meta)",
    """CREATE TABLE IF NOT EXISTS orchestration_control(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), halted INTEGER NOT NULL CHECK(halted IN (0,1)),
        changed_at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
        control_epoch INTEGER NOT NULL DEFAULT 0 CHECK(control_epoch >= 0))""",
    "INSERT OR IGNORE INTO orchestration_control(singleton,halted,changed_at,actor,reason) VALUES(1,1,'1970-01-01T00:00:00.000000Z','system','fail-closed initial state')",
    """CREATE TABLE IF NOT EXISTS risk_policy_revisions(
        revision_id TEXT PRIMARY KEY, policy_version TEXT NOT NULL, policy_json TEXT NOT NULL,
        policy_digest TEXT NOT NULL, approval_status TEXT NOT NULL CHECK(approval_status IN ('PENDING','APPROVED','REVOKED')),
        effective_at TEXT NOT NULL, recorded_at TEXT NOT NULL, reason TEXT NOT NULL)""",
    "CREATE TRIGGER IF NOT EXISTS risk_policy_revisions_no_update BEFORE UPDATE ON risk_policy_revisions BEGIN SELECT RAISE(ABORT,'Risk policy revisions are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS risk_policy_revisions_no_delete BEFORE DELETE ON risk_policy_revisions BEGIN SELECT RAISE(ABORT,'Risk policy revisions are immutable'); END",
    """CREATE TABLE IF NOT EXISTS risk_policy_control(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), active_revision_id TEXT NULL
        REFERENCES risk_policy_revisions(revision_id))""",
    "INSERT OR IGNORE INTO risk_policy_control(singleton,active_revision_id) VALUES(1,NULL)",
    """CREATE TABLE IF NOT EXISTS trade_proposals(
        proposal_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('PROPOSED','RISK_REJECTED','RISK_REVIEW_REQUIRED','RISK_ACCEPTED','POLICY_REJECTED','POLICY_ACCEPTED','PAPER_PENDING','PAPER_COMPLETED','FAILED')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS risk_decisions(
        decision_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES trade_proposals(proposal_id),
        payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, result TEXT NOT NULL, recorded_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS risk_evaluation_receipts(
        receipt_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES trade_proposals(proposal_id),
        request_hash TEXT NOT NULL, proposal_digest TEXT NOT NULL, policy_revision_id TEXT NOT NULL,
        policy_digest TEXT NOT NULL, portfolio_snapshot_id TEXT NOT NULL, portfolio_revision INTEGER NOT NULL,
        portfolio_digest TEXT NOT NULL, market_snapshot_id TEXT NOT NULL, market_revision INTEGER NOT NULL,
        market_digest TEXT NOT NULL, decision_json TEXT NOT NULL, decision_digest TEXT NOT NULL,
        control_epoch INTEGER NOT NULL CHECK(control_epoch >= 0), recorded_at TEXT NOT NULL)""",
    "CREATE TRIGGER IF NOT EXISTS risk_evaluation_receipts_no_update BEFORE UPDATE ON risk_evaluation_receipts BEGIN SELECT RAISE(ABORT,'Risk evaluation receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS risk_evaluation_receipts_no_delete BEFORE DELETE ON risk_evaluation_receipts BEGIN SELECT RAISE(ABORT,'Risk evaluation receipts are immutable'); END",
    """CREATE TABLE IF NOT EXISTS policy_decisions(
        decision_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES trade_proposals(proposal_id),
        payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, result TEXT NOT NULL, recorded_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS exposure_reservations(
        proposal_id TEXT PRIMARY KEY REFERENCES trade_proposals(proposal_id), bucket TEXT NOT NULL,
        amount TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('RESERVED','RELEASED')),
        created_at TEXT NOT NULL, released_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_exposure_active ON exposure_reservations(bucket,status)",
    "CREATE TABLE IF NOT EXISTS exposure_buckets(bucket TEXT PRIMARY KEY, limit_amount TEXT NOT NULL, established_at TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS risk_cash_reservations(
        proposal_id TEXT PRIMARY KEY REFERENCES trade_proposals(proposal_id), instrument_id TEXT NOT NULL,
        currency TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
        quantity TEXT NOT NULL, cash_commitment TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('RESERVED','RELEASED')),
        receipt_id TEXT NOT NULL REFERENCES risk_evaluation_receipts(receipt_id), created_at TEXT NOT NULL,
        released_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_risk_cash_active ON risk_cash_reservations(currency,status,side)",
    """CREATE TABLE IF NOT EXISTS risk_quantity_reservations(
        proposal_id TEXT PRIMARY KEY REFERENCES trade_proposals(proposal_id), instrument_id TEXT NOT NULL,
        quantity TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('RESERVED','RELEASED')),
        receipt_id TEXT NOT NULL REFERENCES risk_evaluation_receipts(receipt_id), created_at TEXT NOT NULL,
        released_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_risk_quantity_active ON risk_quantity_reservations(instrument_id,status)",
    """CREATE TABLE IF NOT EXISTS orchestration_outbox(
        outbox_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES trade_proposals(proposal_id),
        event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('PENDING','CLAIMED','DELIVERED')),
        created_at TEXT NOT NULL, lease_until TEXT, delivered_at TEXT)""",
    "CREATE TABLE IF NOT EXISTS orchestration_events(event_id TEXT PRIMARY KEY, proposal_id TEXT, event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, payload_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS orchestration_event_keys(event_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, result_json TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS passkey_credentials(
        credential_id TEXT PRIMARY KEY, public_key BLOB NOT NULL, user_handle TEXT NOT NULL,
        sign_count INTEGER NOT NULL CHECK(sign_count >= 0), transports_json TEXT NOT NULL,
        created_at TEXT NOT NULL, last_used_at TEXT)""",
    "CREATE TRIGGER IF NOT EXISTS passkey_credentials_protect_identity BEFORE UPDATE ON passkey_credentials WHEN NEW.credential_id!=OLD.credential_id OR NEW.public_key!=OLD.public_key OR NEW.user_handle!=OLD.user_handle OR NEW.transports_json!=OLD.transports_json OR NEW.created_at!=OLD.created_at OR NEW.sign_count<OLD.sign_count OR (OLD.last_used_at IS NOT NULL AND (NEW.last_used_at IS NULL OR NEW.last_used_at<OLD.last_used_at)) BEGIN SELECT RAISE(ABORT,'Passkey credential identity is immutable and counters cannot decrease'); END",
    """CREATE TABLE IF NOT EXISTS webauthn_challenges(
        challenge_id TEXT PRIMARY KEY, challenge TEXT NOT NULL UNIQUE, purpose TEXT NOT NULL,
        context_json TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        consumed_at TEXT)""",
    "CREATE TRIGGER IF NOT EXISTS webauthn_challenges_one_use BEFORE UPDATE ON webauthn_challenges WHEN OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL OR NEW.challenge_id!=OLD.challenge_id OR NEW.challenge!=OLD.challenge OR NEW.purpose!=OLD.purpose OR NEW.context_json!=OLD.context_json OR NEW.created_at!=OLD.created_at OR NEW.expires_at!=OLD.expires_at BEGIN SELECT RAISE(ABORT,'WebAuthn challenge payload is immutable and consumption is one-use'); END",
    "CREATE INDEX IF NOT EXISTS idx_webauthn_challenges_expiry ON webauthn_challenges(expires_at,consumed_at)",
    """CREATE TABLE IF NOT EXISTS risk_policy_approval_receipts(
        receipt_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL REFERENCES risk_policy_revisions(revision_id),
        policy_digest TEXT NOT NULL, readiness_digest TEXT NOT NULL,
        control_epoch INTEGER NOT NULL CHECK(control_epoch >= 0),
        credential_id TEXT NOT NULL REFERENCES passkey_credentials(credential_id),
        challenge_id TEXT NOT NULL UNIQUE REFERENCES webauthn_challenges(challenge_id),
        assertion_digest TEXT NOT NULL, recorded_at TEXT NOT NULL, receipt_digest TEXT NOT NULL)""",
    "CREATE TRIGGER IF NOT EXISTS risk_policy_approval_receipts_no_update BEFORE UPDATE ON risk_policy_approval_receipts BEGIN SELECT RAISE(ABORT,'Risk policy approval receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS risk_policy_approval_receipts_no_delete BEFORE DELETE ON risk_policy_approval_receipts BEGIN SELECT RAISE(ABORT,'Risk policy approval receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS risk_policy_approval_receipts_require_verified_context BEFORE INSERT ON risk_policy_approval_receipts WHEN NOT EXISTS(SELECT 1 FROM webauthn_challenges c WHERE c.challenge_id=NEW.challenge_id AND c.purpose='approve-risk-policy' AND c.consumed_at IS NOT NULL) OR NOT EXISTS(SELECT 1 FROM risk_policy_revisions p WHERE p.revision_id=NEW.revision_id AND p.policy_digest=NEW.policy_digest) BEGIN SELECT RAISE(ABORT,'Risk policy approval receipt lacks a consumed challenge or matching immutable policy'); END",
)

_EXPECTED_COLUMNS = {
    "orchestration_meta": {"version"},
    "orchestration_control": {"singleton", "halted", "changed_at", "actor", "reason", "control_epoch"},
    "risk_policy_revisions": {"revision_id", "policy_version", "policy_json", "policy_digest", "approval_status", "effective_at", "recorded_at", "reason"},
    "risk_policy_control": {"singleton", "active_revision_id"},
    "trade_proposals": {"proposal_id", "idempotency_key", "payload_json", "payload_hash", "state", "created_at", "updated_at"},
    "risk_decisions": {"decision_id", "proposal_id", "payload_json", "payload_hash", "result", "recorded_at"},
    "risk_evaluation_receipts": {"receipt_id", "proposal_id", "request_hash", "proposal_digest", "policy_revision_id", "policy_digest", "portfolio_snapshot_id", "portfolio_revision", "portfolio_digest", "market_snapshot_id", "market_revision", "market_digest", "decision_json", "decision_digest", "control_epoch", "recorded_at"},
    "policy_decisions": {"decision_id", "proposal_id", "payload_json", "payload_hash", "result", "recorded_at"},
    "exposure_reservations": {"proposal_id", "bucket", "amount", "status", "created_at", "released_at"},
    "exposure_buckets": {"bucket", "limit_amount", "established_at"},
    "risk_cash_reservations": {"proposal_id", "instrument_id", "currency", "side", "quantity", "cash_commitment", "status", "receipt_id", "created_at", "released_at"},
    "risk_quantity_reservations": {"proposal_id", "instrument_id", "quantity", "status", "receipt_id", "created_at", "released_at"},
    "orchestration_outbox": {"outbox_id", "proposal_id", "event_type", "payload_json", "status", "created_at", "lease_until", "delivered_at"},
    "orchestration_events": {"event_id", "proposal_id", "event_type", "occurred_at", "payload_json"},
    "orchestration_event_keys": {"event_key", "request_hash", "result_json"},
    "passkey_credentials": {"credential_id", "public_key", "user_handle", "sign_count", "transports_json", "created_at", "last_used_at"},
    "webauthn_challenges": {"challenge_id", "challenge", "purpose", "context_json", "created_at", "expires_at", "consumed_at"},
    "risk_policy_approval_receipts": {"receipt_id", "revision_id", "policy_digest", "readiness_digest", "control_epoch", "credential_id", "challenge_id", "assertion_digest", "recorded_at", "receipt_digest"},
}
_REQUIRED_SCHEMA_OBJECTS = set(_EXPECTED_COLUMNS) | {
    "idx_exposure_active", "idx_risk_cash_active", "idx_risk_quantity_active",
    "risk_policy_revisions_no_update", "risk_policy_revisions_no_delete",
    "risk_evaluation_receipts_no_update", "risk_evaluation_receipts_no_delete",
    "idx_webauthn_challenges_expiry",
    "passkey_credentials_protect_identity", "webauthn_challenges_one_use",
    "risk_policy_approval_receipts_no_update", "risk_policy_approval_receipts_no_delete",
    "risk_policy_approval_receipts_require_verified_context",
}
_PASSKEY_SCHEMA_OBJECTS = {
    "passkey_credentials", "webauthn_challenges", "risk_policy_approval_receipts",
    "passkey_credentials_protect_identity", "webauthn_challenges_one_use",
    "idx_webauthn_challenges_expiry", "risk_policy_approval_receipts_no_update",
    "risk_policy_approval_receipts_no_delete", "risk_policy_approval_receipts_require_verified_context",
}


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Financial values must be finite Decimal instances.")
        return {"$decimal": format(value, "f")}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timestamps must be timezone-aware.")
        return {"$datetime": value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")}
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _canonical(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, float):
        raise ValueError("Floating point values are not accepted in persisted contracts.")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Unsupported persisted type: {type(value).__name__}")


def _dump(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _available_cash_after_commitments(accounting_cash: Decimal, active_buy_commitments: tuple[Decimal, ...],
                                      proposed_commitment: Decimal) -> Decimal:
    """Pure reservation arithmetic; inputs are testable examples, never authority."""
    values = (accounting_cash, proposed_commitment, *active_buy_commitments)
    if any(not isinstance(value, Decimal) or not value.is_finite() or value < 0 for value in values):
        raise ValueError("Cash and commitments must be finite non-negative Decimal values.")
    return accounting_cash - sum(active_buy_commitments, Decimal("0")) - proposed_commitment


class OrchestrationStore:
    """SQLite workflow projection, reservation ledger, event log and outbox."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
        finally:
            db.close()
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        """Atomically create or migrate every orchestration DDL object and seed."""
        with self._connection() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            meta_exists = "orchestration_meta" in tables
            if not meta_exists and tables.intersection(_EXPECTED_COLUMNS):
                raise RuntimeError("Orchestration tables exist without schema metadata; refusing initialization.")

            version: int | None = None
            if meta_exists:
                versions = [row[0] for row in db.execute("SELECT version FROM orchestration_meta")]
                if len(versions) != 1 or versions[0] not in {1, 2, 3, 4}:
                    raise RuntimeError("Unsupported or ambiguous orchestration schema version; refusing initialization.")
                version = versions[0]
                self._validate_existing_columns(db, tables, version)
                if version in {3, 4}:
                    self._validate_complete_schema(db, include_passkey=version == 4)
                    self._validate_control_state(db, has_epoch=True)
                    self._validate_inactive_policy_registry(db)

            # Every statement is executed separately under this BEGIN IMMEDIATE.
            # sqlite3.executescript() is intentionally forbidden here because it
            # commits any pending transaction before running the script.
            for statement in _SCHEMA_STATEMENTS:
                db.execute(statement)

            columns = {row[1] for row in db.execute("PRAGMA table_info(orchestration_control)")}
            if "control_epoch" not in columns:
                db.execute("ALTER TABLE orchestration_control ADD COLUMN control_epoch INTEGER NOT NULL DEFAULT 0 CHECK(control_epoch >= 0)")
            if version in {1, 2, 3}:
                db.execute("UPDATE orchestration_meta SET version=4")

            self._validate_complete_schema(db, include_passkey=True)
            self._validate_control_state(db, has_epoch=True)
            self._validate_inactive_policy_registry(db)
            final_versions = [row[0] for row in db.execute("SELECT version FROM orchestration_meta")]
            if final_versions != [4]:
                raise RuntimeError("Schema migration did not produce exactly version 4.")

    @staticmethod
    def _validate_existing_columns(db: sqlite3.Connection, tables: set[str], version: int) -> None:
        if version == 1 and "orchestration_control" not in tables:
            raise RuntimeError("Schema v1 has no persisted kill-switch row; refusing migration.")
        for table in tables.intersection(_EXPECTED_COLUMNS):
            actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            expected = _EXPECTED_COLUMNS[table]
            if table == "orchestration_control" and version == 1:
                expected = expected - {"control_epoch"}
            if not expected.issubset(actual):
                raise RuntimeError(f"Existing table {table} has an incompatible shape; refusing migration.")

    @staticmethod
    def _validate_complete_schema(db: sqlite3.Connection, *, include_passkey: bool) -> None:
        objects = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        required = _REQUIRED_SCHEMA_OBJECTS if include_passkey else _REQUIRED_SCHEMA_OBJECTS - _PASSKEY_SCHEMA_OBJECTS
        expected_columns = _EXPECTED_COLUMNS if include_passkey else {
            name: columns for name, columns in _EXPECTED_COLUMNS.items() if name not in _PASSKEY_SCHEMA_OBJECTS
        }
        missing = required - objects
        if missing:
            raise RuntimeError("Orchestration schema is incomplete: " + ", ".join(sorted(missing)))
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, expected in expected_columns.items():
            if not expected.issubset({row[1] for row in db.execute(f"PRAGMA table_info({table})")}):
                raise RuntimeError(f"Orchestration table {table} is incomplete.")
            if table not in tables:
                raise RuntimeError(f"Orchestration table {table} is missing.")

    @staticmethod
    def _validate_control_state(db: sqlite3.Connection, *, has_epoch: bool) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(orchestration_control)")}
        if has_epoch and "control_epoch" not in columns:
            raise RuntimeError("control_epoch is missing; refusing to open orchestration.")
        names = "singleton,halted,changed_at,actor,reason" + (",control_epoch" if has_epoch else "")
        rows = db.execute(f"SELECT {names} FROM orchestration_control").fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise RuntimeError("Orchestration control state is missing or ambiguous; refusing to start.")
        row = rows[0]
        if row[1] not in (0, 1) or not all(isinstance(row[index], str) for index in (2, 3, 4)):
            raise RuntimeError("Orchestration control state is invalid; refusing to start.")
        if has_epoch and (not isinstance(row[5], int) or row[5] < 0):
            raise RuntimeError("Orchestration control epoch is invalid; refusing to start.")

    @staticmethod
    def _validate_inactive_policy_registry(db: sqlite3.Connection) -> None:
        rows = db.execute("SELECT active_revision_id FROM risk_policy_control WHERE singleton=1").fetchall()
        if len(rows) != 1 or rows[0][0] is not None:
            raise RuntimeError("RiskPolicy registry must remain inactive during schema initialization.")
        revisions = db.execute("SELECT policy_json,policy_digest,approval_status FROM risk_policy_revisions").fetchall()
        for row in revisions:
            if row[2] not in {"PENDING", "APPROVED", "REVOKED"} or _hash(row[0]) != row[1]:
                raise RuntimeError("RiskPolicy registry content or status is inconsistent; refusing initialization.")

    @classmethod
    def from_root(cls, root: str | Path) -> "OrchestrationStore":
        """Open the shared operational runtime DB, never the Knowledge Core DB."""
        return cls(Path(root).resolve() / "data" / "runtime" / "runtime.sqlite3")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _reader(self):
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _event(db: sqlite3.Connection, proposal_id: str | None, event_type: str, payload: dict) -> None:
        db.execute("INSERT INTO orchestration_events VALUES(?,?,?,?,?)",
                   (new_id(), proposal_id, event_type, iso_utc(), _dump(payload)))

    @staticmethod
    def _state(db: sqlite3.Connection, proposal_id: str) -> str:
        row = db.execute("SELECT state FROM trade_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if row is None:
            raise JobStateError("Proposal inconnue.")
        return row["state"]

    @staticmethod
    def _set_state(db: sqlite3.Connection, proposal_id: str, expected: str, target: WorkflowState) -> None:
        updated = db.execute("UPDATE trade_proposals SET state=?,updated_at=? WHERE proposal_id=? AND state=?",
                             (target.value, iso_utc(), proposal_id, expected))
        if updated.rowcount != 1:
            raise JobStateError(f"Transition interdite : {expected} -> {target.value}.")

    @staticmethod
    def _assert_not_halted(db: sqlite3.Connection) -> None:
        row = db.execute("SELECT halted,control_epoch FROM orchestration_control WHERE singleton=1").fetchone()
        if row is None or row[0] not in (0, 1) or not isinstance(row[1], int) or row[1] < 0:
            raise KillSwitchActive("État de contrôle absent ou incohérent : orchestration arrêtée par défaut.")
        if row[0]:
            raise KillSwitchActive("Kill switch actif : orchestration arrêtée.")

    @staticmethod
    def _idempotent(db: sqlite3.Connection, event_key: str, request_hash: str) -> str | None:
        row = db.execute("SELECT request_hash,result_json FROM orchestration_event_keys WHERE event_key=?", (event_key,)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise OrchestrationConflict("Une clé d’événement a été rejouée avec un contenu différent.")
        return row["result_json"]

    def activate_kill_switch(self, *, actor: str, reason: str) -> None:
        if not actor.strip() or not reason.strip():
            raise ValueError("Un acteur et un motif sont requis.")
        with self._connection() as db:
            db.execute("UPDATE orchestration_control SET halted=1,changed_at=?,actor=?,reason=?,control_epoch=control_epoch+1 WHERE singleton=1",
                       (iso_utc(), actor[:120], reason[:500]))
            state = db.execute("SELECT control_epoch FROM orchestration_control WHERE singleton=1").fetchone()
            if state is None:
                raise KillSwitchActive("État de contrôle absent après activation.")
            self._event(db, None, "KillSwitchActivated", {"actor": actor[:120], "reason": reason[:500],
                                                            "control_epoch": state[0]})

    def clear_kill_switch(self, *, actor: str, reason: str) -> None:
        if not actor.strip() or not reason.strip():
            raise ValueError("La reprise exige une action explicite avec acteur et motif.")
        with self._connection() as db:
            db.execute("UPDATE orchestration_control SET halted=0,changed_at=?,actor=?,reason=?,control_epoch=control_epoch+1 WHERE singleton=1",
                       (iso_utc(), actor[:120], reason[:500]))
            state = db.execute("SELECT control_epoch FROM orchestration_control WHERE singleton=1").fetchone()
            if state is None:
                raise KillSwitchActive("État de contrôle absent après reprise.")
            self._event(db, None, "KillSwitchCleared", {"actor": actor[:120], "reason": reason[:500],
                                                        "control_epoch": state[0]})

    def control_state(self) -> OrchestrationControlState:
        """Read halt and epoch together; corrupt or missing state is a hard veto."""
        with self._reader() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(orchestration_control)")}
            if "control_epoch" not in columns:
                raise KillSwitchActive("Epoch de contrôle indisponible : orchestration arrêtée par défaut.")
            row = db.execute(
                "SELECT halted,control_epoch,changed_at,actor,reason FROM orchestration_control WHERE singleton=1"
            ).fetchone()
            if (row is None or row[0] not in (0, 1) or not isinstance(row[1], int) or row[1] < 0
                    or not all(isinstance(row[index], str) for index in (2, 3, 4))):
                raise KillSwitchActive("État de contrôle absent ou incohérent : orchestration arrêtée par défaut.")
            return OrchestrationControlState(bool(row[0]), row[1], row[2], row[3], row[4])

    def kill_switch_active(self) -> bool:
        return self.control_state().halted

    def submit_proposal(self, proposal: TradeProposal) -> str:
        if not isinstance(proposal, TradeProposal):
            raise TypeError("TradeProposal typé requis.")
        raw = _dump(proposal)
        digest = _hash(raw)
        with self._connection() as db:
            self._assert_not_halted(db)
            if not isinstance(proposal.proposal_id, str) or not proposal.proposal_id.strip():
                raise ValueError("proposal_id est obligatoire.")
            if not isinstance(proposal.idempotency_key, str) or not proposal.idempotency_key.strip():
                raise ValueError("idempotency_key est obligatoire.")
            row = db.execute("SELECT proposal_id,payload_hash FROM trade_proposals WHERE idempotency_key=?",
                             (proposal.idempotency_key,)).fetchone()
            if row:
                if row["payload_hash"] != digest:
                    raise OrchestrationConflict("idempotency_key déjà utilisée pour une proposition différente.")
                return row["proposal_id"]
            if db.execute("SELECT 1 FROM trade_proposals WHERE proposal_id=?", (proposal.proposal_id,)).fetchone():
                raise OrchestrationConflict("proposal_id déjà utilisé.")
            now = iso_utc()
            db.execute("INSERT INTO trade_proposals VALUES(?,?,?,?,?,?,?)",
                       (proposal.proposal_id, proposal.idempotency_key, raw, digest,
                        WorkflowState.PROPOSED.value, now, now))
            self._event(db, proposal.proposal_id, "TradeProposalPersisted", {"payload_hash": digest})
            return proposal.proposal_id

    def record_risk_decision(self, decision: RiskDecision, *, exposure_bucket: str,
                             exposure_limit: Decimal) -> str:
        raise JobStateError(
            "Écriture Risk refusée : une décision fournie par l’appelant n’est jamais une autorité. "
            "Utiliser evaluate_and_record_risk() avec les seules références de la proposition."
        )

    def _load_trusted_risk_inputs(self, db: sqlite3.Connection, market_snapshot_ref: str,
                                  evaluated_at: datetime) -> None:
        """Resolve every operational input inside the Core boundary or fail closed.

        RiskPolicyRegistry has no approved active revision today; the portfolio projection
        is explicitly fixture-only, and DATA can only report a risk-price candidate,
        not provider/license approval. No caller values may replace these authorities.
        """
        del db, market_snapshot_ref, evaluated_at
        raise TrustedRiskUnavailable(
            "Sources Risk opérationnelles manquantes : aucune politique approuvée, aucun portefeuille "
            "autoritaire et aucune autorisation provider/licence marché ne sont configurés."
        )

    def evaluate_and_record_risk(self, proposal_id: str, *, market_snapshot_ref: str) -> RiskEvaluationReceipt:
        """Resolve trusted inputs, evaluate Risk, then persist receipt and reservations atomically.

        Caller-supplied decisions, policies, runtimes, portfolios, cash balances and
        evaluation timestamps are intentionally absent from this API. The current Core
        configuration has no approved policy/portfolio/market authority, so this method
        fails closed before evaluation or any receipt/reservation write.
        """
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            raise ValueError("proposal_id est obligatoire.")
        if not isinstance(market_snapshot_ref, str) or not market_snapshot_ref.strip():
            raise ValueError("market_snapshot_ref est obligatoire.")
        with self._connection() as db:
            self._assert_not_halted(db)
            proposal_row = db.execute(
                "SELECT payload_json,payload_hash,state FROM trade_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if proposal_row is None:
                raise JobStateError("Proposal inconnue.")
            if _hash(proposal_row["payload_json"]) != proposal_row["payload_hash"]:
                raise OrchestrationConflict("Le digest de la proposition persistée est invalide.")
            evaluated_at = utc_now()
            self._load_trusted_risk_inputs(db, market_snapshot_ref, evaluated_at)

    def record_policy_decision(self, decision: PolicyDecision) -> str:
        raise JobStateError(
            "Écriture Policy refusée tant qu’elle ne consomme pas un reçu Risk courant provenant du Core."
        )

    def queue_paper(self, proposal_id: str) -> str:
        """PAPER dispatch remains intentionally unavailable in this foundation."""
        raise JobStateError("PAPER reste désactivé : aucun chemin de reçu Risk/Policy autoritaire n’est validé.")

    def claim_outbox(self, *, lease_seconds: int = 60, now: datetime | None = None) -> dict | None:
        """Fail closed: legacy outbox rows have no trusted Risk receipt/epoch binding."""
        raise JobStateError(
            "Outbox désactivée : les anciens messages ne sont liés à aucun reçu Risk autoritaire ni control_epoch."
        )

    def acknowledge_outbox(self, outbox_id: str) -> None:
        """Fail closed without reading or mutating legacy outbox state."""
        raise JobStateError(
            "ACK outbox désactivé : aucun message historique ne peut progresser sans autorisation Risk vérifiable."
        )

    def state(self, proposal_id: str) -> WorkflowState:
        with self._reader() as db:
            return WorkflowState(self._state(db, proposal_id))

    def events(self, proposal_id: str | None = None) -> list[dict]:
        with self._reader() as db:
            if proposal_id is None:
                rows = db.execute("SELECT * FROM orchestration_events ORDER BY occurred_at,event_id").fetchall()
            else:
                rows = db.execute("SELECT * FROM orchestration_events WHERE proposal_id=? ORDER BY occurred_at,event_id", (proposal_id,)).fetchall()
        return [{"event_id": row["event_id"], "proposal_id": row["proposal_id"], "event_type": row["event_type"],
                 "occurred_at": row["occurred_at"], "payload": json.loads(row["payload_json"])} for row in rows]
