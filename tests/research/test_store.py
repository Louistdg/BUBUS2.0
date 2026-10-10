from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from invest_ai.research import (
    AvailabilityBasis,
    LicenseStatus,
    ResearchDocumentVersion,
    ResearchEvidenceError,
    ResearchStore,
    ResearchStoreError,
    SourceTrust,
    backup_research_database,
    build_snapshot,
    create_evidence_ref,
    create_parser_run,
    restore_research_backup,
    verify_research_database,
)
from invest_ai.research.backup import ResearchBackupError


UTC = timezone.utc
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def fixture_document(native_id: str = "internal-fixture-01", *, available: datetime | None = T0 - timedelta(minutes=5),
                     body: bytes = b"Internal synthetic evidence. No market facts.",
                     license_status: LicenseStatus = LicenseStatus.ALLOWED) -> ResearchDocumentVersion:
    return ResearchDocumentVersion(
        source_id="SYNTHETIC_INTERNAL", source_native_id=native_id, source_version="v1",
        issuer_id="SYNTHETIC-ISSUER", document_type="TEST_FIXTURE",
        original_filename="evidence.txt", mime_type="text/plain", original_bytes=body,
        published_at=T0 - timedelta(days=1), available_at=available,
        availability_basis=AvailabilityBasis.SYNTHETIC_CLOCK if available else AvailabilityBasis.UNKNOWN,
        license_decision_id="LICENSE-SYNTHETIC-INTERNAL-ONLY", license_status=license_status,
        source_trust=SourceTrust.SYNTHETIC_FIXTURE,
    )


class ResearchStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "data" / "research" / "research.sqlite3"
        self.store = ResearchStore(self.path)
        self.doc = fixture_document()
        self.store.store_document(self.doc)
        self.parser = create_parser_run(self.doc, parser_version="fixture-parser-v1",
                                        normalized_text="Synthetic text says 42 units; it is fictional.")
        self.store.store_parser_run(self.parser)
        self.late = fixture_document("internal-fixture-late", available=T0 + timedelta(seconds=1))
        self.snapshot = build_snapshot([self.doc, self.late], as_of=T0)
        self.store.store_snapshot(self.snapshot)
        excerpt = "Synthetic text says 42 units;"
        self.evidence = create_evidence_ref(self.snapshot, self.doc, parser_run=self.parser,
                                            start=0, end=len(excerpt))
        self.store.store_evidence_ref(self.evidence)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_records_round_trip_idempotently_and_verify_as_a_graph(self) -> None:
        self.assertEqual(self.store.store_document(self.doc), self.doc.document_version_id)
        self.assertEqual(self.store.store_parser_run(self.parser), self.parser.parser_run_id)
        self.assertEqual(self.store.store_snapshot(self.snapshot), self.snapshot.snapshot_id)
        self.assertEqual(self.store.store_evidence_ref(self.evidence), self.evidence.evidence_id)
        self.assertEqual(self.store.get_document(self.doc.document_version_id), self.doc)
        self.assertEqual(self.store.get_parser_run(self.parser.parser_run_id), self.parser)
        self.assertEqual(self.store.get_snapshot(self.snapshot.snapshot_id), self.snapshot)
        self.assertEqual(self.store.get_evidence_ref(self.evidence.evidence_id), self.evidence)
        state = self.store.verify()
        self.assertEqual(state["integrity_check"], "ok")
        self.assertEqual(state["row_counts"], {
            "document_versions": 1, "parser_runs": 1, "snapshots": 1,
            "snapshot_documents": 1, "snapshot_exclusions": 1, "evidence_refs": 1,
        })

    def test_database_is_append_only_for_evidence_records(self) -> None:
        statements = (
            "UPDATE document_versions SET raw_sha256=raw_sha256",
            "DELETE FROM document_versions",
            "UPDATE parser_runs SET normalized_text=normalized_text",
            "DELETE FROM parser_runs",
            "UPDATE snapshots SET manifest_json=manifest_json",
            "DELETE FROM snapshots",
            "UPDATE snapshot_documents SET document_version_id=document_version_id",
            "DELETE FROM snapshot_documents",
            "UPDATE snapshot_exclusions SET reason=reason",
            "DELETE FROM snapshot_exclusions",
            "UPDATE evidence_refs SET quoted_text=quoted_text",
            "DELETE FROM evidence_refs",
        )
        db = sqlite3.connect(self.path)
        try:
            for statement in statements:
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement)
                    db.commit()
                db.rollback()
        finally:
            db.close()
        self.assertEqual(self.store.verify()["integrity_check"], "ok")

    def test_store_refuses_non_synthetic_or_unlicensed_documents(self) -> None:
        unlicensed = fixture_document("unknown-license", license_status=LicenseStatus.UNKNOWN)
        with self.assertRaisesRegex(ResearchStoreError, "synthetic fixtures only"):
            self.store.store_document(unlicensed)
        altered_metadata = replace(self.doc, issuer_id="SYNTHETIC-OTHER-ISSUER")
        self.assertEqual(altered_metadata.document_version_id, self.doc.document_version_id)
        with self.assertRaisesRegex(ResearchStoreError, "identity conflict"):
            self.store.store_document(altered_metadata)

    def test_document_and_snapshot_dependencies_must_exist_before_insert(self) -> None:
        isolated = ResearchStore(self.root / "other.sqlite3")
        with self.assertRaisesRegex(ResearchStoreError, "absent or differs"):
            isolated.store_snapshot(self.snapshot)

    def test_integrity_verifier_detects_database_blob_tampering(self) -> None:
        db = sqlite3.connect(self.path)
        try:
            db.execute("DROP TRIGGER document_versions_no_update")
            db.execute("UPDATE document_versions SET original_bytes=? WHERE document_version_id=?",
                       (sqlite3.Binary(b"tampered"), self.doc.document_version_id))
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(ResearchStoreError, "identity verification"):
            verify_research_database(self.path)

    def test_store_refuses_to_initialize_over_an_unrelated_database(self) -> None:
        unrelated = self.root / "not-research.sqlite3"
        db = sqlite3.connect(unrelated)
        db.execute("CREATE TABLE customer_data(secret TEXT)")
        db.commit()
        db.close()
        with self.assertRaisesRegex(ResearchStoreError, "unrelated SQLite"):
            ResearchStore(unrelated)
        check = sqlite3.connect(unrelated)
        try:
            names = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            check.close()
        self.assertEqual(names, {"customer_data"})


class ResearchBackupTests(unittest.TestCase):
    def test_backup_restore_reproduces_store_and_leaves_source_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "research.sqlite3"
            store = ResearchStore(source)
            item = fixture_document()
            store.store_document(item)
            parser = create_parser_run(item, parser_version="fixture-v1", normalized_text="synthetic point")
            store.store_parser_run(parser)
            snapshot = build_snapshot([item], as_of=T0)
            store.store_snapshot(snapshot)
            ref = create_evidence_ref(snapshot, item, parser_run=parser, start=0, end=9)
            store.store_evidence_ref(ref)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            fixed_time = datetime(2026, 10, 9, 13, tzinfo=UTC)
            backup = backup_research_database(source, root / "backups", created_at=fixed_time)
            after = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["row_counts"]["evidence_refs"], 1)
            destination = root / "restored" / "research.sqlite3"
            result = restore_research_backup(backup, destination)
            self.assertEqual(result["integrity_check"], "ok")
            restored = ResearchStore(destination)
            self.assertEqual(restored.counts(), store.counts())
            self.assertEqual(restored.get_evidence_ref(ref.evidence_id), ref)

    def test_backup_refuses_bad_created_time_and_duplicate_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "research.sqlite3"
            ResearchStore(source)
            with self.assertRaisesRegex(ResearchBackupError, "timezone"):
                backup_research_database(source, root / "backups", created_at=datetime(2026, 10, 9))
            stamp = datetime(2026, 10, 9, 14, tzinfo=UTC)
            backup_research_database(source, root / "backups", created_at=stamp)
            with self.assertRaisesRegex(ResearchBackupError, "already exists"):
                backup_research_database(source, root / "backups", created_at=stamp)

    def test_restore_rejects_hash_manifest_and_existing_destination_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "research.sqlite3"
            store = ResearchStore(source)
            store.store_document(fixture_document())
            backup = backup_research_database(source, root / "backups",
                                              created_at=datetime(2026, 10, 9, 15, tzinfo=UTC))
            restored = root / "restored.sqlite3"
            restored.write_bytes(b"keep existing file")
            with self.assertRaisesRegex(ResearchBackupError, "already exist"):
                restore_research_backup(backup, restored)
            restored.unlink()
            database = backup / "research.sqlite3"
            database.write_bytes(database.read_bytes() + b"tamper")
            with self.assertRaisesRegex(ResearchBackupError, "hash"):
                restore_research_backup(backup, restored)
            self.assertFalse(restored.exists())

    def test_restore_rejects_manifest_row_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "research.sqlite3"
            ResearchStore(source).store_document(fixture_document())
            backup = backup_research_database(source, root / "backups",
                                              created_at=datetime(2026, 10, 9, 16, tzinfo=UTC))
            manifest_path = backup / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["row_counts"]["document_versions"] = 0
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ResearchBackupError, "schema or rows differ"):
                restore_research_backup(backup, root / "restore.sqlite3")


if __name__ == "__main__":
    unittest.main()
