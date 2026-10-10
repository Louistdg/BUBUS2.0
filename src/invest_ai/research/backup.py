"""Verified SQLite backup and restore for the isolated Research store."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from invest_ai.research.store import ResearchStoreError, verify_research_database


class ResearchBackupError(ValueError):
    """Research backup or restore failed closed."""


def _iso_utc(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ResearchBackupError("created_at must include a timezone")
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ResearchBackupError("Research database is missing")
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=20)
    db.execute("PRAGMA query_only=ON")
    return db


def _copy_database(source: Path, destination: Path) -> None:
    src = _readonly(source)
    dst = sqlite3.connect(destination, timeout=20)
    try:
        src.backup(dst, pages=256, sleep=0.01)
        dst.commit()
        checkpoint = dst.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and checkpoint[0] != 0:
            raise ResearchBackupError("checkpoint of Research backup copy failed")
        mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise ResearchBackupError("Research backup copy is not standalone")
    except sqlite3.Error as exc:
        raise ResearchBackupError(f"SQLite copy failed: {exc}") from exc
    finally:
        dst.close()
        src.close()
    Path(str(destination) + "-wal").unlink(missing_ok=True)
    Path(str(destination) + "-shm").unlink(missing_ok=True)


def backup_research_database(source_path: str | Path, backups_dir: str | Path, *,
                             created_at: datetime | None = None) -> Path:
    """Create an atomic, hash-manifested backup without mutating the source DB."""
    source = Path(source_path).resolve()
    root = Path(backups_dir).resolve()
    if not source.is_file():
        raise ResearchBackupError("Research database is missing")
    try:
        source_state = verify_research_database(source)
    except ResearchStoreError as exc:
        raise ResearchBackupError(f"source Research database failed verification: {exc}") from exc
    stamp = _iso_utc(created_at)
    safe_stamp = stamp.replace(":", "").replace("-", "")
    final_dir = root / f"research-{safe_stamp}"
    if final_dir.exists():
        raise ResearchBackupError("backup destination already exists")
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".research-backup-", dir=root))
    try:
        database_copy = staging / "research.sqlite3"
        _copy_database(source, database_copy)
        copied_state = verify_research_database(database_copy)
        if copied_state != source_state:
            raise ResearchBackupError("Research backup verification differs from source")
        manifest = {
            "format_version": 1,
            "created_at_utc": stamp,
            "source_database": str(source),
            "database_file": "research.sqlite3",
            "database_sha256": _sha256(database_copy),
            "schema_version": source_state["schema_version"],
            "row_counts": source_state["row_counts"],
            "sqlite_version": sqlite3.sqlite_version,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, final_dir)
        return final_dir
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def restore_research_backup(backup_dir: str | Path, destination_path: str | Path) -> dict[str, object]:
    """Restore into a new path, checking file hash, schema, rows, and evidence graph."""
    directory = Path(backup_dir).resolve()
    destination = Path(destination_path).resolve()
    manifest_path = directory / "manifest.json"
    source = directory / "research.sqlite3"
    if not manifest_path.is_file() or not source.is_file():
        raise ResearchBackupError("backup must contain manifest.json and research.sqlite3")
    if destination.exists():
        raise ResearchBackupError("restore destination must not already exist")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchBackupError("backup manifest cannot be read") from exc
    if (not isinstance(manifest, dict) or manifest.get("format_version") != 1
            or manifest.get("database_file") != "research.sqlite3"
            or not isinstance(manifest.get("database_sha256"), str)):
        raise ResearchBackupError("backup manifest has an unsupported shape")
    if _sha256(source) != manifest["database_sha256"]:
        raise ResearchBackupError("backup database hash does not match its manifest")
    try:
        source_state = verify_research_database(source)
    except ResearchStoreError as exc:
        raise ResearchBackupError(f"backup database failed verification: {exc}") from exc
    if (source_state.get("schema_version") != manifest.get("schema_version")
            or source_state.get("row_counts") != manifest.get("row_counts")):
        raise ResearchBackupError("backup database schema or rows differ from manifest")

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".restore.tmp",
                                     dir=destination.parent)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        _copy_database(source, temporary)
        restored_state = verify_research_database(temporary)
        if restored_state != source_state:
            raise ResearchBackupError("restored Research database failed verification")
        os.replace(temporary, destination)
        return {"destination": str(destination), "schema_version": restored_state["schema_version"],
                "row_counts": restored_state["row_counts"], "integrity_check": "ok",
                "database_sha256": _sha256(destination)}
    except Exception:
        temporary.unlink(missing_ok=True)
        Path(str(temporary) + "-wal").unlink(missing_ok=True)
        Path(str(temporary) + "-shm").unlink(missing_ok=True)
        raise
