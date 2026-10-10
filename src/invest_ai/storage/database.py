"""SQLite storage and schema for the local INVEST-AI knowledge core."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA busy_timeout = 30000")
    return db


def initialize(db: sqlite3.Connection) -> None:
    """Idempotently create the versioned document and lexical index schema."""
    db.executescript("""
    CREATE TABLE IF NOT EXISTS schema_info (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        schema_version INTEGER NOT NULL
    );
    INSERT INTO schema_info(singleton, schema_version) VALUES(1, 1)
      ON CONFLICT(singleton) DO UPDATE SET schema_version=excluded.schema_version;
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL CHECK(source_type IN
          ('official','documentation','paper','github','forum','codex_report','user_document','other')),
        source_name TEXT NOT NULL,
        original_path TEXT NOT NULL,
        url TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(source_type, original_path)
    );
    CREATE TABLE IF NOT EXISTS documents (
        document_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        language TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        current_version INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS document_sources (
        document_id TEXT NOT NULL REFERENCES documents(document_id),
        source_id TEXT NOT NULL REFERENCES sources(source_id),
        PRIMARY KEY(document_id, source_id)
    );
    CREATE TABLE IF NOT EXISTS document_versions (
        document_version_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES documents(document_id),
        source_id TEXT NOT NULL REFERENCES sources(source_id),
        version INTEGER NOT NULL,
        original_path TEXT NOT NULL,
        original_stored_path TEXT NOT NULL,
        normalized_path TEXT NOT NULL,
        url TEXT,
        published_at TEXT,
        retrieved_at TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        sha256 TEXT NOT NULL UNIQUE,
        mime_type TEXT NOT NULL,
        language TEXT NOT NULL,
        status TEXT NOT NULL,
        UNIQUE(document_id, version)
    );
    CREATE INDEX IF NOT EXISTS idx_versions_document ON document_versions(document_id, version);
    CREATE TABLE IF NOT EXISTS chunks (
        chunk_id TEXT PRIMARY KEY,
        document_version_id TEXT NOT NULL REFERENCES document_versions(document_version_id),
        ordinal INTEGER NOT NULL,
        heading TEXT,
        line_start INTEGER NOT NULL,
        line_end INTEGER NOT NULL,
        text TEXT NOT NULL,
        text_sha256 TEXT NOT NULL,
        UNIQUE(document_version_id, ordinal)
    );
    CREATE TABLE IF NOT EXISTS ingestion_runs (
        ingestion_run_id TEXT PRIMARY KEY,
        source_path TEXT NOT NULL,
        source_type TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        document_id TEXT,
        document_version_id TEXT,
        chunks_created INTEGER NOT NULL DEFAULT 0,
        message TEXT
    );
    CREATE TABLE IF NOT EXISTS index_versions (
        index_name TEXT NOT NULL,
        version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        implementation TEXT NOT NULL,
        status TEXT NOT NULL,
        PRIMARY KEY(index_name, version)
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        chunk_id UNINDEXED, heading, text, tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks BEGIN
      INSERT INTO chunks_fts(chunk_id, heading, text) VALUES(new.chunk_id, coalesce(new.heading,''), new.text);
    END;
    CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
      DELETE FROM chunks_fts WHERE chunk_id = old.chunk_id;
    END;
    CREATE TRIGGER IF NOT EXISTS chunks_fts_update AFTER UPDATE ON chunks BEGIN
      DELETE FROM chunks_fts WHERE chunk_id = old.chunk_id;
      INSERT INTO chunks_fts(chunk_id, heading, text) VALUES(new.chunk_id, coalesce(new.heading,''), new.text);
    END;
    INSERT OR IGNORE INTO index_versions(index_name, version, created_at, implementation, status)
      VALUES('lexical_fts5', 1, strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'SQLite FTS5 unicode61', 'active');
    """)
    db.commit()


def fts5_available(db: sqlite3.Connection) -> bool:
    try:
        db.execute("SELECT count(*) FROM chunks_fts").fetchone()
        return True
    except sqlite3.OperationalError:
        return False
