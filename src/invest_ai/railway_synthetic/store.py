"""Disposable projection store; callers must provide a temp-only file path."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .contract import ContractError, validate_snapshot


class IngestConflict(RuntimeError):
    def __init__(self, code: str, message: str, *, corrupt: bool = False):
        super().__init__(message)
        self.code = code
        self.corrupt = corrupt


class SyntheticProjectionStore:
    """SQLite test adapter, isolated to an explicitly supplied temporary path."""

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] | None = None, fail_before_commit: bool = False):
        self.path = Path(path).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        try:
            self.path.relative_to(temp_root)
        except ValueError as exc:
            raise ValueError("synthetic store must live under the operating-system temporary directory") from exc
        if self.path.name.casefold() == "coordination.sqlite3":
            raise ValueError("operational coordination database filename is forbidden")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fail_before_commit = fail_before_commit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS synthetic_stream(
                    source_id TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL,
                    head_hash TEXT NOT NULL,
                    received_at_utc TEXT NOT NULL,
                    state TEXT NOT NULL,
                    handshake_ok INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS synthetic_snapshots(
                    sequence INTEGER PRIMARY KEY,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    snapshot_hash TEXT NOT NULL UNIQUE,
                    previous_hash TEXT,
                    created_at_utc TEXT NOT NULL,
                    received_at_utc TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
            """)
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _read_projection(self, db: sqlite3.Connection) -> tuple[dict | None, sqlite3.Row | None, sqlite3.Row | None, bool]:
        """Read content and its stream head from one consistent SQLite snapshot."""
        db.execute("BEGIN")
        stream = db.execute("SELECT * FROM synthetic_stream ORDER BY sequence DESC LIMIT 1").fetchone()
        snapshot_row = None
        if stream is not None:
            snapshot_row = db.execute("SELECT * FROM synthetic_snapshots WHERE sequence=?", (stream["sequence"],)).fetchone()
        if stream is None or snapshot_row is None:
            db.rollback()
            return None, stream, None, False
        if (snapshot_row["snapshot_hash"] != stream["head_hash"]
                or snapshot_row["snapshot_id"] != f"SYN-SNAPSHOT-{stream['sequence']:06d}"):
            db.rollback()
            return None, stream, None, False
        try:
            snapshot = json.loads(snapshot_row["snapshot_json"])
        except (TypeError, json.JSONDecodeError):
            db.rollback()
            return None, stream, snapshot_row, False
        if (snapshot.get("sequence") != stream["sequence"]
                or snapshot.get("snapshot_sha256") != stream["head_hash"]
                or hashlib.sha256(json.dumps({k: v for k, v in snapshot.items() if k != "snapshot_sha256"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() != stream["head_hash"]):
            db.rollback()
            return None, stream, snapshot_row, False
        return snapshot, stream, snapshot_row, True

    @staticmethod
    def _now(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("server clock must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def ingest(self, value: dict) -> dict:
        snapshot = validate_snapshot(value)
        received = self._now(self.clock())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM synthetic_snapshots WHERE sequence=?", (snapshot["sequence"],)).fetchone()
            if existing:
                if existing["snapshot_hash"] == snapshot["snapshot_sha256"] and existing["snapshot_id"] == snapshot["snapshot_id"]:
                    db.rollback()
                    return {"result": "IDEMPOTENT", "sequence": existing["sequence"], "snapshot_hash": existing["snapshot_hash"], "received_at_utc": existing["received_at_utc"]}
                db.execute("UPDATE synthetic_stream SET state='CORRUPT',handshake_ok=0 WHERE source_id=?", (snapshot["source_instance_id"],))
                db.commit()
                raise IngestConflict("SNAPSHOT_CONFLICT", "same sequence has different content", corrupt=True)

            head = db.execute("SELECT * FROM synthetic_stream WHERE source_id=?", (snapshot["source_instance_id"],)).fetchone()
            expected_sequence = 1 if head is None else int(head["sequence"]) + 1
            expected_parent = None if head is None else head["head_hash"]
            if snapshot["sequence"] != expected_sequence:
                db.rollback()
                raise IngestConflict("SEQUENCE_GAP" if snapshot["sequence"] > expected_sequence else "STALE_SEQUENCE", "snapshot sequence is not the next expected value")
            if snapshot["previous_snapshot_hash"] != expected_parent:
                db.rollback()
                raise IngestConflict("HEAD_MISMATCH", "previous snapshot hash does not match current head")

            db.execute("INSERT INTO synthetic_snapshots VALUES(?,?,?,?,?,?,?)", (
                snapshot["sequence"], snapshot["snapshot_id"], snapshot["snapshot_sha256"],
                snapshot["previous_snapshot_hash"], snapshot["created_at_utc"], received,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ))
            db.execute("""INSERT INTO synthetic_stream VALUES(?,?,?,?,?,1)
                ON CONFLICT(source_id) DO UPDATE SET sequence=excluded.sequence,head_hash=excluded.head_hash,
                received_at_utc=excluded.received_at_utc,state='FRESH',handshake_ok=1""",
                (snapshot["source_instance_id"], snapshot["sequence"], snapshot["snapshot_sha256"], received, "FRESH"))
            if self.fail_before_commit:
                raise sqlite3.OperationalError("injected failure before commit")
            db.commit()
            return {"result": "ACCEPTED", "sequence": snapshot["sequence"], "snapshot_hash": snapshot["snapshot_sha256"], "received_at_utc": received}
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def handshake(self, source_id: str, sequence: int, head_hash: str | None) -> str:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            stream = db.execute("SELECT * FROM synthetic_stream WHERE source_id=?", (source_id,)).fetchone()
            if stream is None:
                db.rollback()
                return "UNKNOWN"
            if sequence > stream["sequence"]:
                state = "SOURCE_AHEAD"
            elif sequence < stream["sequence"]:
                state = "PROJECTION_AHEAD"
            elif head_hash != stream["head_hash"]:
                state = "HEAD_MISMATCH"
            else:
                state = "MATCH"
            db.execute("UPDATE synthetic_stream SET handshake_ok=? WHERE source_id=?", (1 if state == "MATCH" else 0, source_id))
            db.commit()
            return state
        finally:
            db.close()

    def snapshot(self) -> dict | None:
        db = self._connect()
        try:
            snapshot, _stream, _row, _consistent = self._read_projection(db)
            return snapshot
        finally:
            db.close()

    def projection(self, *, now: datetime | None = None, freshness_seconds: float = 120, after_read=None) -> tuple[dict, dict | None]:
        """Return one atomically read snapshot plus metadata derived from its same head."""
        current = self._now(now or self.clock())
        db = self._connect()
        try:
            snapshot, stream, snapshot_row, consistent = self._read_projection(db)
            if after_read is not None:
                after_read()
        finally:
            db.close()
        if stream is None:
            state, age = "UNKNOWN", None
            sequence, head, received = None, None, None
        elif not consistent:
            state, age = "UNKNOWN", None
            sequence, head, received = stream["sequence"], stream["head_hash"], stream["received_at_utc"]
        else:
            received_at = stream["received_at_utc"] if snapshot is None else snapshot_row["received_at_utc"]
            received_dt = datetime.strptime(received_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            current_dt = datetime.strptime(current, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            delta = (current_dt - received_dt).total_seconds()
            age = None if delta < 0 else delta
            if delta < 0:
                state = "UNKNOWN"
            elif stream["state"] == "CORRUPT":
                state = "UNKNOWN"
            elif delta > freshness_seconds:
                state = "STALE"
            elif not stream["handshake_ok"]:
                state = "UNKNOWN"
            else:
                state = "FRESH"
            sequence, head, received = stream["sequence"], stream["head_hash"], received_at
        metadata = {
            "sync_state": state,
            "dataset_mode": "SYNTHETIC_ONLY",
            "source_authority": "NONE",
            "source_instance_id": "SYNTH-SOURCE-RAILWAY-DEMO-01" if stream else None,
            "snapshot_sequence": sequence,
            "snapshot_hash": head,
            "received_at_utc": received,
            "age_seconds": age,
            "freshness_max_seconds": freshness_seconds,
            "simulation_label": "SIMULATION",
        }
        if stream is not None and snapshot is None:
            metadata["sync_state"] = "UNKNOWN"
        return metadata, snapshot

    def health(self, *, now: datetime | None = None, freshness_seconds: float = 120) -> dict:
        metadata, _snapshot = self.projection(now=now, freshness_seconds=freshness_seconds)
        return metadata
