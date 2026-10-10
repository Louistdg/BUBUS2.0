"""Append-only local SQLite persistence for synthetic Research evidence.

The store is intentionally separate from Knowledge, Runtime, Coordination,
Risk, Portfolio, Gateway, and execution databases. It has no network access.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from invest_ai.research.evidence import (
    AvailabilityBasis,
    EvidenceRef,
    ExcludedDocument,
    LicenseStatus,
    ParserRun,
    ResearchDocumentVersion,
    ResearchSnapshot,
    SourceTrust,
    canonical_json,
    verify_evidence_ref,
    verify_parser_run,
    verify_snapshot,
)


SCHEMA_VERSION = 1
DEFAULT_DATABASE = Path(__file__).resolve().parents[3] / "data" / "research" / "research.sqlite3"
_TABLES = ("document_versions", "parser_runs", "snapshots", "snapshot_documents",
           "snapshot_exclusions", "evidence_refs")

_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS schema_info (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_info(singleton,schema_version) VALUES(1,1);
CREATE TABLE IF NOT EXISTS document_versions (
  document_version_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  original_bytes BLOB NOT NULL,
  raw_sha256 TEXT NOT NULL,
  byte_length INTEGER NOT NULL CHECK(byte_length > 0)
);
CREATE INDEX IF NOT EXISTS idx_research_doc_identity ON document_versions(document_id);
CREATE TABLE IF NOT EXISTS parser_runs (
  parser_run_id TEXT PRIMARY KEY,
  document_version_id TEXT NOT NULL REFERENCES document_versions(document_version_id),
  parser_version TEXT NOT NULL,
  normalized_sha256 TEXT NOT NULL,
  normalized_text TEXT NOT NULL,
  trust_label TEXT NOT NULL CHECK(trust_label='UNTRUSTED_PARSER_OUTPUT')
);
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY,
  manifest_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  as_of TEXT NOT NULL,
  source_basis TEXT NOT NULL,
  selection_policy_version TEXT NOT NULL,
  manifest_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_documents (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  document_version_id TEXT NOT NULL REFERENCES document_versions(document_version_id),
  PRIMARY KEY(snapshot_id, document_version_id)
);
CREATE TABLE IF NOT EXISTS snapshot_exclusions (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  document_version_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, document_version_id, reason)
);
CREATE TABLE IF NOT EXISTS evidence_refs (
  evidence_id TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  document_version_id TEXT NOT NULL REFERENCES document_versions(document_version_id),
  parser_run_id TEXT NOT NULL REFERENCES parser_runs(parser_run_id),
  parser_version TEXT NOT NULL,
  normalized_sha256 TEXT NOT NULL,
  locator_start INTEGER NOT NULL CHECK(locator_start >= 0),
  locator_end INTEGER NOT NULL CHECK(locator_end > locator_start),
  quoted_text TEXT NOT NULL,
  quoted_text_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_snapshot ON evidence_refs(snapshot_id, document_version_id);
CREATE TRIGGER IF NOT EXISTS document_versions_no_update BEFORE UPDATE ON document_versions
BEGIN SELECT RAISE(ABORT,'Research document versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS document_versions_no_delete BEFORE DELETE ON document_versions
BEGIN SELECT RAISE(ABORT,'Research document versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS parser_runs_no_update BEFORE UPDATE ON parser_runs
BEGIN SELECT RAISE(ABORT,'Research parser runs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS parser_runs_no_delete BEFORE DELETE ON parser_runs
BEGIN SELECT RAISE(ABORT,'Research parser runs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT,'Research snapshots are append-only'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT,'Research snapshots are append-only'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_documents_no_update BEFORE UPDATE ON snapshot_documents
BEGIN SELECT RAISE(ABORT,'Research snapshot membership is append-only'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_documents_no_delete BEFORE DELETE ON snapshot_documents
BEGIN SELECT RAISE(ABORT,'Research snapshot membership is append-only'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_exclusions_no_update BEFORE UPDATE ON snapshot_exclusions
BEGIN SELECT RAISE(ABORT,'Research snapshot exclusions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_exclusions_no_delete BEFORE DELETE ON snapshot_exclusions
BEGIN SELECT RAISE(ABORT,'Research snapshot exclusions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_refs_no_update BEFORE UPDATE ON evidence_refs
BEGIN SELECT RAISE(ABORT,'Research evidence references are append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_refs_no_delete BEFORE DELETE ON evidence_refs
BEGIN SELECT RAISE(ABORT,'Research evidence references are append-only'); END;
COMMIT;
"""


class ResearchStoreError(ValueError):
    """Research database operation failed or violated its append-only contract."""


def _parse_time(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchStoreError("stored timestamp has no timezone")
    return parsed


def _document_from_row(row: sqlite3.Row) -> ResearchDocumentVersion:
    try:
        meta = json.loads(row["manifest_json"])
        document = ResearchDocumentVersion(
            source_id=meta["source_id"], source_native_id=meta["source_native_id"],
            source_version=meta["source_version"], issuer_id=meta["issuer_id"],
            document_type=meta["document_type"], original_filename=meta["original_filename"],
            mime_type=meta["mime_type"], original_bytes=bytes(row["original_bytes"]),
            published_at=_parse_time(meta["published_at"]) if meta["published_at"] else None,
            available_at=_parse_time(meta["available_at"]) if meta["available_at"] else None,
            availability_basis=AvailabilityBasis(meta["availability_basis"]),
            license_decision_id=meta["license_decision_id"],
            license_status=LicenseStatus(meta["license_status"]),
            source_trust=SourceTrust(meta["source_trust"]), schema_version=meta["schema_version"],
        )
        if (canonical_json(meta).decode("utf-8") != row["manifest_json"]
                or document.identity_manifest() != meta
                or document.document_id != row["document_id"]
                or document.document_version_id != row["document_version_id"]
                or document.raw_sha256 != row["raw_sha256"]
                or document.byte_length != row["byte_length"]
                or not document.verify_original()):
            raise ResearchStoreError("stored Research document failed identity verification")
        return document
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ResearchStoreError):
            raise
        raise ResearchStoreError("stored Research document metadata is invalid") from exc


def _parser_from_row(row: sqlite3.Row) -> ParserRun:
    return ParserRun(row["parser_run_id"], row["document_version_id"], row["parser_version"],
                     row["normalized_text"], row["normalized_sha256"], row["trust_label"])


def _snapshot_from_row(row: sqlite3.Row) -> ResearchSnapshot:
    try:
        manifest = json.loads(row["manifest_json"])
        return ResearchSnapshot(
            snapshot_id=row["snapshot_id"], manifest_sha256=row["manifest_sha256"],
            manifest_json=row["manifest_json"], as_of=_parse_time(row["as_of"]),
            document_version_ids=tuple(manifest["document_version_ids"]),
            excluded=tuple(ExcludedDocument(item["document_version_id"], item["reason"])
                           for item in manifest["excluded"]),
            source_basis=row["source_basis"],
            selection_policy_version=row["selection_policy_version"],
            manifest_version=row["manifest_version"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ResearchStoreError("stored Research snapshot metadata is invalid") from exc


def _evidence_from_row(row: sqlite3.Row) -> EvidenceRef:
    return EvidenceRef(row["evidence_id"], row["snapshot_id"], row["document_version_id"],
                       row["parser_run_id"], row["parser_version"], row["normalized_sha256"],
                       row["locator_start"], row["locator_end"], row["quoted_text"],
                       row["quoted_text_sha256"])


@contextmanager
def _connection(path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    if readonly:
        if not path.is_file():
            raise ResearchStoreError("Research database does not exist")
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=20)
    else:
        db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=20000")
    if readonly:
        db.execute("PRAGMA query_only=ON")
    try:
        yield db
        if not readonly:
            db.commit()
    except Exception:
        if not readonly:
            db.rollback()
        raise
    finally:
        db.close()


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    return {table: int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) for table in _TABLES}


def verify_research_database(path: str | Path) -> dict[str, object]:
    """Read-only integrity, schema, FK, hash, and typed-record verification."""
    db_path = Path(path).resolve()
    with _connection(db_path, readonly=True) as db:
        try:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ResearchStoreError(f"SQLite integrity_check failed: {integrity}")
            schema = db.execute("SELECT schema_version FROM schema_info WHERE singleton=1").fetchone()
            if schema is None or schema[0] != SCHEMA_VERSION:
                raise ResearchStoreError("unsupported Research database schema version")
            fk_errors = db.execute("PRAGMA foreign_key_check").fetchall()
            if fk_errors:
                raise ResearchStoreError("Research database contains foreign-key violations")
            documents: dict[str, ResearchDocumentVersion] = {}
            for row in db.execute("SELECT * FROM document_versions"):
                doc = _document_from_row(row)
                documents[doc.document_version_id] = doc
            parsers: dict[str, ParserRun] = {}
            for row in db.execute("SELECT * FROM parser_runs"):
                parser = _parser_from_row(row)
                document = documents.get(parser.document_version_id)
                if document is None or not verify_parser_run(document, parser):
                    raise ResearchStoreError("stored parser run failed verification")
                parsers[parser.parser_run_id] = parser
            snapshots: dict[str, ResearchSnapshot] = {}
            for row in db.execute("SELECT * FROM snapshots"):
                snapshot = _snapshot_from_row(row)
                if not verify_snapshot(snapshot):
                    raise ResearchStoreError("stored snapshot failed manifest verification")
                actual_docs = tuple(item[0] for item in db.execute(
                    "SELECT document_version_id FROM snapshot_documents WHERE snapshot_id=? ORDER BY document_version_id",
                    (snapshot.snapshot_id,)))
                if actual_docs != tuple(sorted(snapshot.document_version_ids)):
                    raise ResearchStoreError("stored snapshot membership differs from its manifest")
                actual_exclusions = tuple(ExcludedDocument(item[0], item[1]) for item in db.execute(
                    "SELECT document_version_id,reason FROM snapshot_exclusions WHERE snapshot_id=? ORDER BY document_version_id,reason",
                    (snapshot.snapshot_id,)))
                if actual_exclusions != tuple(sorted(snapshot.excluded, key=lambda item: (item.document_version_id, item.reason))):
                    raise ResearchStoreError("stored snapshot exclusions differ from its manifest")
                for doc_id in snapshot.document_version_ids:
                    doc = documents.get(doc_id)
                    if doc is None or doc.identity_manifest() not in json.loads(snapshot.manifest_json)["documents"]:
                        raise ResearchStoreError("snapshot references an absent or changed document")
                snapshots[snapshot.snapshot_id] = snapshot
            for row in db.execute("SELECT * FROM evidence_refs"):
                ref = _evidence_from_row(row)
                document = documents.get(ref.document_version_id)
                parser = parsers.get(ref.parser_run_id)
                snapshot = snapshots.get(ref.snapshot_id)
                if (document is None or parser is None or snapshot is None
                        or verify_evidence_ref(snapshot, document, parser, ref).value != "PASS_TECHNICAL"):
                    raise ResearchStoreError("stored EvidenceRef failed verification")
            return {"integrity_check": "ok", "schema_version": SCHEMA_VERSION,
                    "row_counts": _counts(db)}
        except sqlite3.Error as exc:
            raise ResearchStoreError(f"Research database verification failed: {exc}") from exc


class ResearchStore:
    """Append-only SQLite store containing only internal synthetic Research data."""

    def __init__(self, path: str | Path = DEFAULT_DATABASE) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing_tables: set[str] = set()
        if self.path.exists() and self.path.stat().st_size:
            try:
                with _connection(self.path, readonly=True) as db:
                    existing_tables = {row[0] for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    if "schema_info" in existing_tables:
                        row = db.execute("SELECT schema_version FROM schema_info WHERE singleton=1").fetchone()
                        if row is None or row[0] != SCHEMA_VERSION:
                            raise ResearchStoreError("unsupported Research database schema version")
                    elif existing_tables:
                        raise ResearchStoreError("refusing to initialize over an unrelated SQLite database")
            except sqlite3.DatabaseError as exc:
                raise ResearchStoreError("existing Research database is not a valid SQLite database") from exc
        with _connection(self.path) as db:
            try:
                db.executescript(_SCHEMA)
                db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error as exc:
                raise ResearchStoreError(f"could not initialize Research database: {exc}") from exc
        self.verify()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with _connection(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def verify(self) -> dict[str, object]:
        return verify_research_database(self.path)

    def store_document(self, document: ResearchDocumentVersion) -> str:
        if not isinstance(document, ResearchDocumentVersion) or not document.verify_original():
            raise ResearchStoreError("document failed original-byte verification")
        if (document.source_trust is not SourceTrust.SYNTHETIC_FIXTURE
                or document.license_status is not LicenseStatus.ALLOWED
                or document.license_decision_id != "LICENSE-SYNTHETIC-INTERNAL-ONLY"):
            raise ResearchStoreError("this store currently accepts internal synthetic fixtures only")
        manifest = canonical_json(document.identity_manifest()).decode("utf-8")
        with self._write() as db:
            old = db.execute("SELECT * FROM document_versions WHERE document_version_id=?",
                             (document.document_version_id,)).fetchone()
            if old:
                if (old["manifest_json"] != manifest
                        or bytes(old["original_bytes"]) != document.original_bytes):
                    raise ResearchStoreError("document-version identity conflict")
                return document.document_version_id
            db.execute("INSERT INTO document_versions VALUES(?,?,?,?,?,?)",
                       (document.document_version_id, document.document_id, manifest,
                        sqlite3.Binary(document.original_bytes), document.raw_sha256, document.byte_length))
        return document.document_version_id

    def store_parser_run(self, parser_run: ParserRun) -> str:
        if not isinstance(parser_run, ParserRun):
            raise ResearchStoreError("parser_run must use the canonical type")
        document = self.get_document(parser_run.document_version_id)
        if document is None or not verify_parser_run(document, parser_run):
            raise ResearchStoreError("parser run does not verify against a stored document")
        with self._write() as db:
            old = db.execute("SELECT * FROM parser_runs WHERE parser_run_id=?", (parser_run.parser_run_id,)).fetchone()
            values = (parser_run.document_version_id, parser_run.parser_version,
                      parser_run.normalized_sha256, parser_run.normalized_text, parser_run.trust_label)
            if old:
                if tuple(old[name] for name in ("document_version_id", "parser_version", "normalized_sha256",
                                                  "normalized_text", "trust_label")) != values:
                    raise ResearchStoreError("parser-run identity conflict")
                return parser_run.parser_run_id
            db.execute("INSERT INTO parser_runs VALUES(?,?,?,?,?,?)",
                       (parser_run.parser_run_id, *values))
        return parser_run.parser_run_id

    def store_snapshot(self, snapshot: ResearchSnapshot) -> str:
        if not isinstance(snapshot, ResearchSnapshot) or not verify_snapshot(snapshot):
            raise ResearchStoreError("snapshot failed manifest verification")
        if snapshot.source_basis not in {"SYNTHETIC_ONLY", "NO_ELIGIBLE_DOCUMENTS"}:
            raise ResearchStoreError("only synthetic-only or empty snapshots are supported by this store")
        for doc_id in snapshot.document_version_ids:
            document = self.get_document(doc_id)
            if (document is None or document.identity_manifest() not in json.loads(snapshot.manifest_json)["documents"]):
                raise ResearchStoreError("snapshot includes a document that is absent or differs from the stored version")
        with self._write() as db:
            values = (snapshot.manifest_sha256, snapshot.manifest_json,
                      snapshot.as_of.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                      snapshot.source_basis, snapshot.selection_policy_version, snapshot.manifest_version)
            old = db.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot.snapshot_id,)).fetchone()
            if old:
                names = ("manifest_sha256", "manifest_json", "as_of", "source_basis",
                         "selection_policy_version", "manifest_version")
                if tuple(old[name] for name in names) != values:
                    raise ResearchStoreError("snapshot identity conflict")
            else:
                db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)", (snapshot.snapshot_id, *values))
            for doc_id in snapshot.document_version_ids:
                db.execute("INSERT OR IGNORE INTO snapshot_documents VALUES(?,?)", (snapshot.snapshot_id, doc_id))
            for excluded in snapshot.excluded:
                db.execute("INSERT OR IGNORE INTO snapshot_exclusions VALUES(?,?,?)",
                           (snapshot.snapshot_id, excluded.document_version_id, excluded.reason))
        return snapshot.snapshot_id

    def store_evidence_ref(self, evidence: EvidenceRef) -> str:
        if not isinstance(evidence, EvidenceRef):
            raise ResearchStoreError("evidence must use the canonical type")
        snapshot = self.get_snapshot(evidence.snapshot_id)
        document = self.get_document(evidence.document_version_id)
        parser = self.get_parser_run(evidence.parser_run_id)
        if (snapshot is None or document is None or parser is None
                or verify_evidence_ref(snapshot, document, parser, evidence).value != "PASS_TECHNICAL"):
            raise ResearchStoreError("EvidenceRef does not verify against stored Research records")
        values = (evidence.snapshot_id, evidence.document_version_id, evidence.parser_run_id,
                  evidence.parser_version, evidence.normalized_sha256, evidence.locator_start,
                  evidence.locator_end, evidence.quoted_text, evidence.quoted_text_sha256)
        with self._write() as db:
            old = db.execute("SELECT * FROM evidence_refs WHERE evidence_id=?", (evidence.evidence_id,)).fetchone()
            names = ("snapshot_id", "document_version_id", "parser_run_id", "parser_version",
                     "normalized_sha256", "locator_start", "locator_end", "quoted_text", "quoted_text_sha256")
            if old:
                if tuple(old[name] for name in names) != values:
                    raise ResearchStoreError("EvidenceRef identity conflict")
                return evidence.evidence_id
            db.execute("INSERT INTO evidence_refs VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (evidence.evidence_id, *values))
        return evidence.evidence_id

    def get_document(self, document_version_id: str) -> ResearchDocumentVersion | None:
        with _connection(self.path, readonly=True) as db:
            row = db.execute("SELECT * FROM document_versions WHERE document_version_id=?",
                             (document_version_id,)).fetchone()
            return _document_from_row(row) if row else None

    def get_parser_run(self, parser_run_id: str) -> ParserRun | None:
        with _connection(self.path, readonly=True) as db:
            row = db.execute("SELECT * FROM parser_runs WHERE parser_run_id=?", (parser_run_id,)).fetchone()
            if row is None:
                return None
            parser = _parser_from_row(row)
            document_row = db.execute("SELECT * FROM document_versions WHERE document_version_id=?",
                                      (parser.document_version_id,)).fetchone()
            if document_row is None or not verify_parser_run(_document_from_row(document_row), parser):
                raise ResearchStoreError("stored parser run failed verification")
            return parser

    def get_snapshot(self, snapshot_id: str) -> ResearchSnapshot | None:
        with _connection(self.path, readonly=True) as db:
            row = db.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            if row is None:
                return None
            snapshot = _snapshot_from_row(row)
            if not verify_snapshot(snapshot):
                raise ResearchStoreError("stored snapshot failed verification")
            return snapshot

    def get_evidence_ref(self, evidence_id: str) -> EvidenceRef | None:
        with _connection(self.path, readonly=True) as db:
            row = db.execute("SELECT * FROM evidence_refs WHERE evidence_id=?", (evidence_id,)).fetchone()
            if row is None:
                return None
            evidence = _evidence_from_row(row)
            snapshot_row = db.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (evidence.snapshot_id,)).fetchone()
            document_row = db.execute("SELECT * FROM document_versions WHERE document_version_id=?",
                                      (evidence.document_version_id,)).fetchone()
            parser_row = db.execute("SELECT * FROM parser_runs WHERE parser_run_id=?",
                                    (evidence.parser_run_id,)).fetchone()
            if not all((snapshot_row, document_row, parser_row)):
                raise ResearchStoreError("stored EvidenceRef has missing dependencies")
            if verify_evidence_ref(_snapshot_from_row(snapshot_row), _document_from_row(document_row),
                                   _parser_from_row(parser_row), evidence).value != "PASS_TECHNICAL":
                raise ResearchStoreError("stored EvidenceRef failed verification")
            return evidence

    def counts(self) -> dict[str, int]:
        with _connection(self.path, readonly=True) as db:
            return _counts(db)
