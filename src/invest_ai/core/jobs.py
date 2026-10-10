"""Minimal durable job/event store separate from the read-only Knowledge Core."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from invest_ai.core.clock import iso_utc, parse_utc, to_utc, utc_now
from invest_ai.core.errors import JobStateError
from invest_ai.core.ids import new_id
from invest_ai.core.logging import redact

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_schema (version INTEGER NOT NULL);
INSERT INTO runtime_schema(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM runtime_schema);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','COMPLETED','FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  available_at TEXT NOT NULL,
  lease_until TEXT,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, available_at, created_at);
CREATE TABLE IF NOT EXISTS job_attempts (
  attempt_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  attempt_number INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,
  error TEXT,
  UNIQUE(job_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS runtime_events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  aggregate_id TEXT,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runtime_events_time ON runtime_events(occurred_at, event_id);
"""


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    kind: str
    payload: dict
    idempotency_key: str
    status: str
    attempts: int
    max_attempts: int
    created_at: datetime
    available_at: datetime
    lease_until: datetime | None


class RuntimeStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _validate_payload(payload: dict) -> str:
        if not isinstance(payload, dict):
            raise ValueError("Le payload de job/event doit être un objet JSON.")
        secret_key = re.compile(r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|private[_-]?key|credential)")

        def inspect(value) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if secret_key.search(str(key)):
                        raise ValueError("Un payload runtime ne peut pas contenir de champ secret.")
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)

        inspect(payload)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"], kind=row["kind"], payload=json.loads(row["payload_json"]),
            idempotency_key=row["idempotency_key"], status=row["status"], attempts=row["attempts"],
            max_attempts=row["max_attempts"], created_at=parse_utc(row["created_at"]),
            available_at=parse_utc(row["available_at"]),
            lease_until=parse_utc(row["lease_until"]) if row["lease_until"] else None,
        )

    def enqueue(self, kind: str, payload: dict, idempotency_key: str, *, max_attempts: int = 5, available_at: datetime | None = None) -> str:
        if not kind.strip() or not idempotency_key.strip():
            raise ValueError("kind et idempotency_key sont obligatoires.")
        if max_attempts < 1:
            raise ValueError("max_attempts doit être supérieur à zéro.")
        serialized = self._validate_payload(payload)
        job_id = new_id()
        now = iso_utc()
        due = iso_utc(available_at or utc_now())
        with self._connection() as db:
            db.execute("INSERT OR IGNORE INTO jobs(job_id,kind,payload_json,idempotency_key,status,max_attempts,created_at,available_at) VALUES(?,?,?,?, 'PENDING',?,?,?)",
                       (job_id, kind, serialized, idempotency_key, max_attempts, now, due))
            row = db.execute("SELECT job_id,kind,payload_json,max_attempts FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if (row["kind"], row["payload_json"], row["max_attempts"]) != (kind, serialized, max_attempts):
                raise JobStateError("Conflit : la même clé d’idempotence désigne un job différent.")
            return row["job_id"]

    def append_event(self, event_type: str, payload: dict, *, aggregate_id: str | None = None, occurred_at: datetime | None = None) -> str:
        if not event_type.strip():
            raise ValueError("event_type est obligatoire.")
        serialized = self._validate_payload(payload)
        event_id = new_id()
        with self._connection() as db:
            db.execute("INSERT INTO runtime_events(event_id,event_type,occurred_at,aggregate_id,payload_json) VALUES(?,?,?,?,?)",
                       (event_id, event_type, iso_utc(occurred_at), aggregate_id, serialized))
        return event_id

    def recover_expired(self, *, now: datetime | None = None) -> int:
        instant = iso_utc(now)
        with self._connection() as db:
            return self._recover_expired(db, instant)

    @staticmethod
    def _recover_expired(db: sqlite3.Connection, instant: str) -> int:
        expired = db.execute("SELECT job_id,attempts FROM jobs WHERE status='RUNNING' AND lease_until<=?", (instant,)).fetchall()
        for row in expired:
            db.execute("UPDATE job_attempts SET finished_at=?,outcome='LEASE_EXPIRED',error='Worker lease expired' WHERE job_id=? AND attempt_number=? AND finished_at IS NULL",
                       (instant, row["job_id"], row["attempts"]))
        if not expired:
            return 0
        result = db.execute("UPDATE jobs SET status='PENDING',lease_until=NULL,available_at=? WHERE status='RUNNING' AND lease_until<=?",
                            (instant, instant))
        return result.rowcount

    def claim_next(self, *, now: datetime | None = None, lease_seconds: int = 300) -> Job | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds doit être positif.")
        moment = to_utc(now or utc_now())
        now_text = iso_utc(moment)
        lease_text = iso_utc(moment + timedelta(seconds=lease_seconds))
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover_expired(db, now_text)
            row = db.execute("SELECT * FROM jobs WHERE status='PENDING' AND available_at<=? ORDER BY available_at,created_at,job_id LIMIT 1", (now_text,)).fetchone()
            if row is None:
                return None
            attempt = row["attempts"] + 1
            updated = db.execute("UPDATE jobs SET status='RUNNING',attempts=?,lease_until=? WHERE job_id=? AND status='PENDING'",
                                 (attempt, lease_text, row["job_id"]))
            if updated.rowcount != 1:
                return None
            db.execute("INSERT INTO job_attempts(attempt_id,job_id,attempt_number,started_at) VALUES(?,?,?,?)",
                       (new_id(), row["job_id"], attempt, now_text))
            claimed = db.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            return self._job(claimed)

    def complete(self, job_id: str, *, now: datetime | None = None) -> None:
        moment = iso_utc(now)
        with self._connection() as db:
            row = db.execute("SELECT attempts,status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["status"] != "RUNNING":
                raise JobStateError("Seul un job RUNNING connu peut être terminé.")
            db.execute("UPDATE jobs SET status='COMPLETED',lease_until=NULL,last_error=NULL WHERE job_id=?", (job_id,))
            db.execute("UPDATE job_attempts SET finished_at=?,outcome='COMPLETED' WHERE job_id=? AND attempt_number=?",
                       (moment, job_id, row["attempts"]))

    def fail(self, job_id: str, error: str, *, retryable: bool, now: datetime | None = None, retry_base_seconds: int = 2, retry_max_seconds: int = 300) -> str:
        moment = to_utc(now or utc_now())
        safe_error = redact(error)[:500]
        with self._connection() as db:
            row = db.execute("SELECT attempts,max_attempts,status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["status"] != "RUNNING":
                raise JobStateError("Seul un job RUNNING connu peut échouer.")
            should_retry = retryable and row["attempts"] < row["max_attempts"]
            if should_retry:
                delay = min(retry_base_seconds * (2 ** max(0, row["attempts"] - 1)), retry_max_seconds)
                available = iso_utc(moment + timedelta(seconds=delay))
                status = "PENDING"
                db.execute("UPDATE jobs SET status=?,lease_until=NULL,available_at=?,last_error=? WHERE job_id=?",
                           (status, available, safe_error, job_id))
            else:
                status = "FAILED"
                db.execute("UPDATE jobs SET status=?,lease_until=NULL,last_error=? WHERE job_id=?", (status, safe_error, job_id))
            db.execute("UPDATE job_attempts SET finished_at=?,outcome=?,error=? WHERE job_id=? AND attempt_number=?",
                       (iso_utc(moment), status, safe_error, job_id, row["attempts"]))
            return status

    def get_job(self, job_id: str) -> Job | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._job(row) if row else None

    def events(self, event_type: str | None = None) -> list[dict]:
        with self._connection() as db:
            if event_type:
                rows = db.execute("SELECT * FROM runtime_events WHERE event_type=? ORDER BY occurred_at,event_id", (event_type,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM runtime_events ORDER BY occurred_at,event_id").fetchall()
        return [{"event_id": row["event_id"], "event_type": row["event_type"], "occurred_at": row["occurred_at"],
                 "aggregate_id": row["aggregate_id"], "payload": json.loads(row["payload_json"])} for row in rows]
