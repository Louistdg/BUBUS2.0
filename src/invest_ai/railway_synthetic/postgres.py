"""Optional PostgreSQL projection adapter; no driver is imported by default.

The caller supplies a DB-API connection factory. This module never discovers
the Coordination Core database or reads deployment environment variables.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .contract import SOURCE_ID, validate_snapshot
from .store import IngestConflict


MIGRATION_DIR = Path(__file__).with_name("migrations")
MIGRATIONS = ((1, MIGRATION_DIR / "001_initial.sql"),)


class PostgresProjectionStore:
    """PostgreSQL implementation over a caller-provided DB-API connector.

    ``connect`` must connect only to an explicitly configured disposable
    synthetic database. No DSN, account or secret is created or inferred here.
    """

    def __init__(self, connect: Callable[[], object], *, clock: Callable[[], datetime] | None = None):
        if not callable(connect):
            raise TypeError("a PostgreSQL DB-API connection factory is required")
        self._connect = connect
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._continuity_verified = False

    @staticmethod
    def _close(connection: object, cursor: object | None = None) -> None:
        if cursor is not None:
            cursor.close()
        connection.close()

    @staticmethod
    def _mapping(cursor: object, row: object) -> dict | None:
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        names = [column[0] for column in cursor.description]
        return dict(zip(names, row))

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("server clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @classmethod
    def _utc_text(cls, value: datetime) -> str:
        return cls._utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def migrate(self) -> None:
        """Apply ordered, versioned migrations atomically and idempotently."""
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("BEGIN")
            cursor.execute("SELECT pg_advisory_xact_lock(762941033)")
            cursor.execute("""CREATE TABLE IF NOT EXISTS synthetic_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at_utc TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            cursor.execute("SELECT version FROM synthetic_schema_migrations")
            applied = {int(row[0]) for row in cursor.fetchall()}
            known_versions = {version for version, _path in MIGRATIONS}
            if applied - known_versions:
                raise RuntimeError("unknown synthetic PostgreSQL migration version")
            for version, path in MIGRATIONS:
                if version in applied:
                    continue
                sql = path.read_text(encoding="utf-8")
                for statement in (part.strip() for part in sql.split(";")):
                    if statement:
                        cursor.execute(statement)
                cursor.execute("INSERT INTO synthetic_schema_migrations(version) VALUES (%s)", (version,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._close(connection, cursor)

    def ingest(self, value: dict) -> dict:
        snapshot = validate_snapshot(value)
        received = self._utc(self.clock())
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("BEGIN")
            cursor.execute("SELECT version FROM synthetic_schema_migrations")
            applied = {int(row[0]) for row in cursor.fetchall()}
            if not {version for version, _path in MIGRATIONS}.issubset(applied):
                raise RuntimeError("synthetic PostgreSQL migrations have not been applied")
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", (SOURCE_ID,))
            cursor.execute("""SELECT source_instance_id, schema_version, last_sequence, head_hash,
                       last_received_at_utc, stream_state, handshake_ok
                       FROM synthetic_streams WHERE source_instance_id=%s FOR UPDATE""", (SOURCE_ID,))
            stream = self._mapping(cursor, cursor.fetchone())
            cursor.execute("""SELECT snapshot_id, snapshot_hash, received_at_utc
                       FROM synthetic_snapshots WHERE source_instance_id=%s AND sequence=%s""",
                           (SOURCE_ID, snapshot["sequence"]))
            existing = self._mapping(cursor, cursor.fetchone())
            if existing:
                if existing["snapshot_hash"].strip() == snapshot["snapshot_sha256"] and existing["snapshot_id"] == snapshot["snapshot_id"]:
                    connection.rollback()
                    return {"result": "IDEMPOTENT", "sequence": snapshot["sequence"], "snapshot_hash": snapshot["snapshot_sha256"], "received_at_utc": self._utc_text(existing["received_at_utc"])}
                cursor.execute("UPDATE synthetic_streams SET stream_state='CORRUPT', handshake_ok=FALSE WHERE source_instance_id=%s", (SOURCE_ID,))
                connection.commit()
                raise IngestConflict("SNAPSHOT_CONFLICT", "same sequence has different content", corrupt=True)

            expected_sequence = 1 if stream is None else int(stream["last_sequence"]) + 1
            expected_parent = None if stream is None else stream["head_hash"].strip()
            if snapshot["sequence"] != expected_sequence:
                raise IngestConflict("SEQUENCE_GAP" if snapshot["sequence"] > expected_sequence else "STALE_SEQUENCE", "snapshot sequence is not the next expected value")
            if snapshot["previous_snapshot_hash"] != expected_parent:
                raise IngestConflict("HEAD_MISMATCH", "previous snapshot hash does not match current head")

            if stream is None:
                cursor.execute("""INSERT INTO synthetic_streams
                    (source_instance_id,schema_version,last_sequence,head_hash,last_received_at_utc,stream_state,handshake_ok)
                    VALUES (%s,%s,%s,%s,%s,'FRESH',FALSE)""",
                    (SOURCE_ID, snapshot["schema_version"], snapshot["sequence"], snapshot["snapshot_sha256"], received))
            else:
                cursor.execute("""UPDATE synthetic_streams SET schema_version=%s,last_sequence=%s,head_hash=%s,
                    last_received_at_utc=%s,stream_state='FRESH',handshake_ok=FALSE WHERE source_instance_id=%s""",
                    (snapshot["schema_version"], snapshot["sequence"], snapshot["snapshot_sha256"], received, SOURCE_ID))
            cursor.execute("""INSERT INTO synthetic_snapshots
                (source_instance_id,sequence,snapshot_id,schema_version,created_at_utc,received_at_utc,previous_hash,snapshot_hash,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (SOURCE_ID, snapshot["sequence"], snapshot["snapshot_id"], snapshot["schema_version"], snapshot["created_at_utc"], received,
                 snapshot["previous_snapshot_hash"], snapshot["snapshot_sha256"], json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))))
            connection.commit()
            return {"result": "ACCEPTED", "sequence": snapshot["sequence"], "snapshot_hash": snapshot["snapshot_sha256"], "received_at_utc": self._utc_text(received)}
        except Exception:
            connection.rollback()
            raise
        finally:
            self._close(connection, cursor)

    def handshake(self, source_id: str, sequence: int, head_hash: str | None) -> str:
        """Verify source continuity after process start without refreshing freshness."""
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("BEGIN")
            cursor.execute("SELECT last_sequence,head_hash FROM synthetic_streams WHERE source_instance_id=%s FOR UPDATE", (SOURCE_ID,))
            stream = self._mapping(cursor, cursor.fetchone())
            if source_id != SOURCE_ID or stream is None:
                result = "UNKNOWN"
            elif sequence > int(stream["last_sequence"]):
                result = "SOURCE_AHEAD"
            elif sequence < int(stream["last_sequence"]):
                result = "PROJECTION_AHEAD"
            elif head_hash != stream["head_hash"].strip():
                result = "HEAD_MISMATCH"
            else:
                result = "MATCH"
            if stream is not None:
                cursor.execute("UPDATE synthetic_streams SET handshake_ok=%s WHERE source_instance_id=%s", (result == "MATCH", SOURCE_ID))
            connection.commit()
            self._continuity_verified = result == "MATCH"
            return result
        except Exception:
            connection.rollback()
            self._continuity_verified = False
            raise
        finally:
            self._close(connection, cursor)

    def projection(self, *, now: datetime | None = None, freshness_seconds: float = 120, after_read=None) -> tuple[dict, dict | None]:
        """Read stream and payload at one repeatable-read MVCC snapshot."""
        current = self._utc(now or self.clock())
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("""SELECT source_instance_id,schema_version,last_sequence,head_hash,last_received_at_utc,
                       stream_state,handshake_ok FROM synthetic_streams WHERE source_instance_id=%s""", (SOURCE_ID,))
            stream = self._mapping(cursor, cursor.fetchone())
            snapshot = None
            if stream is not None:
                cursor.execute("""SELECT sequence,snapshot_id,snapshot_hash,received_at_utc,payload
                    FROM synthetic_snapshots WHERE source_instance_id=%s AND sequence=%s""", (SOURCE_ID, stream["last_sequence"]))
                row = self._mapping(cursor, cursor.fetchone())
                if row and row["snapshot_hash"].strip() == stream["head_hash"].strip() and row["snapshot_id"] == f"SYN-SNAPSHOT-{stream['last_sequence']:06d}":
                    snapshot = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
                    try:
                        validate_snapshot(snapshot)
                        consistent = snapshot.get("snapshot_sha256") == stream["head_hash"].strip() and snapshot.get("sequence") == stream["last_sequence"]
                    except (TypeError, ValueError):
                        consistent = False
                    received = row["received_at_utc"]
                else:
                    consistent = False
                    received = stream["last_received_at_utc"]
            else:
                consistent = False
                received = None
            if after_read is not None:
                after_read()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._close(connection, cursor)

        if stream is None:
            state, age, sequence, head = "UNKNOWN", None, None, None
            received_text = None
        elif not consistent:
            state, age, sequence, head = "UNKNOWN", None, int(stream["last_sequence"]), stream["head_hash"].strip()
            received_text = self._utc_text(received) if received is not None else None
        else:
            delta = (current - self._utc(received)).total_seconds()
            received_text = self._utc_text(received)
            age = None if delta < 0 else delta
            if delta < 0 or stream["stream_state"] == "CORRUPT" or not stream["handshake_ok"] or not self._continuity_verified:
                state = "UNKNOWN"
            elif delta > freshness_seconds:
                state = "STALE"
            else:
                state = "FRESH"
            sequence, head = int(stream["last_sequence"]), stream["head_hash"].strip()
        metadata = {
            "sync_state": state,
            "dataset_mode": "SYNTHETIC_ONLY",
            "source_authority": "NONE",
            "source_instance_id": SOURCE_ID if stream else None,
            "snapshot_sequence": sequence,
            "snapshot_hash": head,
            "received_at_utc": received_text,
            "age_seconds": age,
            "freshness_max_seconds": freshness_seconds,
            "simulation_label": "SIMULATION",
        }
        return metadata, snapshot


def psycopg_connection_factory(dsn: str) -> Callable[[], object]:
    """Opt-in adapter factory; import psycopg only when explicitly called."""
    if not dsn or not dsn.startswith(("postgresql://", "postgres://")):
        raise ValueError("an explicit PostgreSQL DSN is required")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("install the pinned psycopg dependency in an isolated deployment image first") from exc
    return lambda: psycopg.connect(dsn)
