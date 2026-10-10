"""Coherent, read-only-source SQLite backup and verified restore helpers."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from invest_ai.core.clock import iso_utc, utc_now
from invest_ai.core.errors import BackupError


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise BackupError(f"Base SQLite absente : {path}")
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db
    except sqlite3.Error as exc:
        raise BackupError(f"Ouverture SQLite en lecture seule impossible : {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_standalone_database(db: sqlite3.Connection) -> None:
    """Checkpoint and switch a copied DB to DELETE journaling before publishing it."""
    try:
        checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and checkpoint[0] != 0:
            raise BackupError("Checkpoint WAL de la copie impossible.")
        mode = db.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise BackupError(f"Impossible de rendre la copie autonome (journal_mode={mode}).")
    except sqlite3.Error as exc:
        raise BackupError(f"Normalisation de la copie SQLite impossible : {exc}") from exc


def _schema_version(db: sqlite3.Connection) -> int:
    try:
        row = db.execute("SELECT schema_version FROM schema_info WHERE singleton=1").fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"La table schema_info est absente ou illisible : {exc}") from exc
    if row is None or not isinstance(row[0], int):
        raise BackupError("Version de schéma Knowledge Core introuvable.")
    return row[0]


def _validate_database(path: Path, *, require_fts: bool = True) -> tuple[int, int]:
    db = _readonly_connection(path)
    try:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise BackupError(f"Échec integrity_check : {integrity}")
        version = _schema_version(db)
        if require_fts:
            try:
                count = db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
                # A MATCH query exercises the FTS virtual table's query path as well.
                db.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 1", ("investai_backup_probe",)).fetchall()
            except sqlite3.Error as exc:
                raise BackupError(f"FTS5 absent ou inutilisable : {exc}") from exc
        else:
            count = 0
        return version, count
    finally:
        db.close()


def backup_knowledge_database(source_path: str | Path, backups_dir: str | Path, *, created_at: datetime | None = None) -> Path:
    """Create an atomic backup directory containing DB and a SHA-256 manifest."""
    source = Path(source_path).resolve()
    root = Path(backups_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    created = created_at or utc_now()
    timestamp = iso_utc(created)
    safe_stamp = timestamp.replace(":", "").replace("-", "")
    final_dir = root / f"knowledge-{safe_stamp}"
    if final_dir.exists():
        raise BackupError(f"La destination de sauvegarde existe déjà : {final_dir}")
    version, fts_count = _validate_database(source)
    staging = Path(tempfile.mkdtemp(prefix=".knowledge-backup-", dir=root))
    try:
        database_copy = staging / "knowledge.sqlite3"
        src = _readonly_connection(source)
        dst = sqlite3.connect(database_copy, timeout=30)
        try:
            src.backup(dst, pages=256, sleep=0.01)
            dst.commit()
            _normalize_standalone_database(dst)
        except sqlite3.Error as exc:
            raise BackupError(f"La copie SQLite a échoué : {exc}") from exc
        finally:
            dst.close()
            src.close()
        for suffix in ("-wal", "-shm"):
            Path(str(database_copy) + suffix).unlink(missing_ok=True)

        restored_version, restored_fts_count = _validate_database(database_copy)
        if restored_version != version or restored_fts_count != fts_count:
            raise BackupError("La vérification de la copie diffère de la base source.")
        manifest = {
            "format_version": 1,
            "created_at_utc": timestamp,
            "source_database": str(source),
            "database_file": "knowledge.sqlite3",
            "database_sha256": _sha256(database_copy),
            "schema_version": restored_version,
            "sqlite_version": sqlite3.sqlite_version,
            "fts5_available": True,
            "fts5_rows": restored_fts_count,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, final_dir)
        return final_dir
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def restore_knowledge_backup(backup_dir: str | Path, destination_path: str | Path) -> dict:
    """Restore to a new destination and verify hash, integrity, schema and FTS5."""
    directory = Path(backup_dir).resolve()
    destination = Path(destination_path).resolve()
    manifest_path = directory / "manifest.json"
    source = directory / "knowledge.sqlite3"
    if not manifest_path.is_file() or not source.is_file():
        raise BackupError("La sauvegarde doit contenir knowledge.sqlite3 et manifest.json.")
    if destination.exists():
        raise BackupError(f"La destination de restauration existe déjà : {destination}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"Manifest illisible : {exc}") from exc
    if _sha256(source) != manifest.get("database_sha256"):
        raise BackupError("Le hash de la base ne correspond pas au manifest.")
    version, _ = _validate_database(source)
    if version != manifest.get("schema_version"):
        raise BackupError("La version du schéma ne correspond pas au manifest.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.restore.tmp")
    if temporary.exists():
        raise BackupError(f"Fichier temporaire de restauration déjà présent : {temporary}")
    src = _readonly_connection(source)
    dst = sqlite3.connect(temporary, timeout=30)
    try:
        src.backup(dst, pages=256, sleep=0.01)
        dst.commit()
        _normalize_standalone_database(dst)
    except sqlite3.Error as exc:
        raise BackupError(f"La restauration SQLite a échoué : {exc}") from exc
    finally:
        dst.close()
        src.close()
    for suffix in ("-wal", "-shm"):
        Path(str(temporary) + suffix).unlink(missing_ok=True)
    try:
        restored_version, restored_fts_count = _validate_database(temporary)
        if restored_version != version:
            raise BackupError("Version de schéma restaurée inattendue.")
        if restored_fts_count != manifest.get("fts5_rows"):
            raise BackupError("Le nombre de lignes FTS5 restauré diffère du manifest.")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {"destination": str(destination), "schema_version": restored_version, "fts5_rows": restored_fts_count,
            "integrity_check": "ok", "database_sha256": _sha256(destination)}
