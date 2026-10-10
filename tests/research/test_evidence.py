from __future__ import annotations

import ast
import hashlib
import socket
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from invest_ai.research import (
    AvailabilityBasis,
    CitationCheck,
    LicenseStatus,
    ResearchDocumentVersion,
    ResearchEvidenceError,
    SourceTrust,
    build_snapshot,
    canonical_json,
    create_evidence_ref,
    create_parser_run,
    verify_evidence_ref,
    verify_parser_run,
    verify_snapshot,
)


UTC = timezone.utc
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def document(*, native_id: str = "fixture-doc-01", body: bytes = b"synthetic investment report",
             published: datetime | None = T0 - timedelta(days=1),
             available: datetime | None = T0 - timedelta(hours=1),
             basis: AvailabilityBasis = AvailabilityBasis.SYNTHETIC_CLOCK,
             license_status: LicenseStatus = LicenseStatus.ALLOWED,
             trust: SourceTrust = SourceTrust.SYNTHETIC_FIXTURE,
             license_id: str = "LICENSE-SYNTHETIC-INTERNAL-ONLY") -> ResearchDocumentVersion:
    return ResearchDocumentVersion(
        source_id="SYNTHETIC_INTERNAL", source_native_id=native_id, source_version="fixture-v1",
        issuer_id="SYNTHETIC-COMPANY-01", document_type="DEMO_REPORT",
        original_filename="report.txt", mime_type="text/plain", original_bytes=body,
        published_at=published, available_at=available, availability_basis=basis,
        license_decision_id=license_id, license_status=license_status,
        source_trust=trust,
    )


class CanonicalIdentityTests(unittest.TestCase):
    def test_canonical_json_normalizes_unicode_sorts_keys_and_rejects_float(self) -> None:
        self.assertEqual(canonical_json({"e\u0301": "e\u0301", "a": 1}),
                         '{"a":1,"é":"é"}'.encode())
        with self.assertRaises(ResearchEvidenceError):
            canonical_json({"value": 1.25})
        with self.assertRaises(ResearchEvidenceError):
            canonical_json({"é": 1, "e\u0301": 2})

    def test_document_identity_binds_source_native_version_and_exact_original_bytes(self) -> None:
        first = document()
        duplicate = document()
        revised = document(body=b"revised synthetic report")
        self.assertEqual(first.document_id, duplicate.document_id)
        self.assertEqual(first.document_version_id, duplicate.document_version_id)
        self.assertNotEqual(first.document_version_id, revised.document_version_id)
        self.assertEqual(first.raw_sha256, hashlib.sha256(first.original_bytes).hexdigest())
        self.assertTrue(first.verify_original())
        object.__setattr__(first, "original_bytes", b"tampered")
        self.assertFalse(first.verify_original())

    def test_document_rejects_naive_time_bad_filename_and_unsupported_schema(self) -> None:
        with self.assertRaisesRegex(ResearchEvidenceError, "timezone"):
            document(available=datetime(2026, 10, 9, 11, 0))
        with self.assertRaisesRegex(ResearchEvidenceError, "filename"):
            replace(document(), original_filename="../report.txt")
        with self.assertRaisesRegex(ResearchEvidenceError, "schema_version"):
            replace(document(), schema_version="future-contract")

    def test_availability_basis_and_trust_label_must_agree(self) -> None:
        with self.assertRaisesRegex(ResearchEvidenceError, "authoritative acquisition receipts"):
            document(basis=AvailabilityBasis.OBSERVED)
        with self.assertRaisesRegex(ResearchEvidenceError, "synthetic-fixture"):
            document(trust=SourceTrust.UNVERIFIED, license_status=LicenseStatus.UNKNOWN)
        with self.assertRaisesRegex(ResearchEvidenceError, "published_at"):
            document(published=T0, available=T0 - timedelta(seconds=1))

    def test_callers_cannot_self_certify_real_source_or_license(self) -> None:
        with self.assertRaisesRegex(ResearchEvidenceError, "cannot be self-certified"):
            document(trust=SourceTrust.VERIFIED_OBSERVATION, basis=AvailabilityBasis.OBSERVED)
        with self.assertRaisesRegex(ResearchEvidenceError, "only internal synthetic fixtures"):
            document(license_id="SEC-GREEN", license_status=LicenseStatus.ALLOWED)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_order_independent_and_point_in_time(self) -> None:
        first = document()
        second = document(native_id="fixture-doc-02", available=T0)
        late = document(native_id="fixture-doc-late", available=T0 + timedelta(seconds=1))
        a = build_snapshot([first, second, late], as_of=T0)
        b = build_snapshot([late, second, first], as_of=T0)
        self.assertEqual(a.snapshot_id, b.snapshot_id)
        self.assertEqual(a.document_version_ids, b.document_version_ids)
        self.assertEqual([(row.document_version_id, row.reason) for row in a.excluded],
                         [(late.document_version_id, "STALE_OR_LATE_DATA")])
        self.assertEqual(a.source_basis, "SYNTHETIC_ONLY")
        self.assertTrue(verify_snapshot(a))

    def test_cutoff_and_parser_policy_inputs_change_snapshot_identity(self) -> None:
        item = document()
        before = build_snapshot([item], as_of=T0)
        after = build_snapshot([item], as_of=T0 + timedelta(seconds=1))
        self.assertNotEqual(before.snapshot_id, after.snapshot_id)

    def test_unknown_license_publication_availability_and_source_fail_closed(self) -> None:
        cases = (
            (document(native_id="unknown-license", license_status=LicenseStatus.UNKNOWN), "LICENSE_NOT_AUTHORIZED"),
            (document(native_id="denied-license", license_status=LicenseStatus.DENIED), "LICENSE_NOT_AUTHORIZED"),
            (document(native_id="unknown-published", published=None), "PUBLISHED_AT_UNKNOWN"),
            (document(native_id="unknown-available", available=None, basis=AvailabilityBasis.UNKNOWN), "AS_OF_NOT_PROVABLE"),
            (document(native_id="unverified-source", trust=SourceTrust.UNVERIFIED,
                      basis=AvailabilityBasis.UNKNOWN, available=None,
                      license_status=LicenseStatus.UNKNOWN), "LICENSE_NOT_AUTHORIZED"),
            (document(native_id="future-published", published=T0 + timedelta(seconds=1),
                      available=T0 + timedelta(seconds=1)), "STALE_OR_LATE_DATA"),
        )
        for item, reason in cases:
            with self.subTest(reason=reason):
                snapshot = build_snapshot([item], as_of=T0)
                self.assertEqual(snapshot.document_version_ids, ())
                self.assertEqual(snapshot.excluded[0].reason, reason)
                self.assertEqual(snapshot.source_basis, "NO_ELIGIBLE_DOCUMENTS")
                self.assertTrue(verify_snapshot(snapshot))

    def test_unknown_availability_cannot_be_smuggled_in_with_a_timestamp(self) -> None:
        with self.assertRaisesRegex(ResearchEvidenceError, "unknown availability basis"):
            document(basis=AvailabilityBasis.UNKNOWN, available=T0)

    def test_duplicate_document_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(ResearchEvidenceError, "duplicate document"):
            build_snapshot([document(), document()], as_of=T0)

    def test_snapshot_manifest_tampering_is_detected(self) -> None:
        snapshot = build_snapshot([document()], as_of=T0)
        self.assertFalse(verify_snapshot(replace(snapshot, manifest_json=snapshot.manifest_json + " ")))
        altered_manifest = snapshot.manifest_json.replace("SYNTHETIC_ONLY", "OBSERVED")
        self.assertFalse(verify_snapshot(replace(snapshot, manifest_json=altered_manifest)))
        self.assertFalse(verify_snapshot(replace(snapshot, source_basis="OBSERVED")))
        self.assertFalse(verify_snapshot(replace(snapshot, as_of=datetime(2026, 10, 9, 12))))


class EvidenceReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = document()
        self.snapshot = build_snapshot([self.doc], as_of=T0)
        self.text = "Synthetic revenue: 120 units. No real issuer data."
        self.parser_run = create_parser_run(self.doc, parser_version="fixture-parser-v1",
                                            normalized_text=self.text)
        self.ref = create_evidence_ref(self.snapshot, self.doc, parser_run=self.parser_run,
                                       start=0, end=len("Synthetic revenue: 120 units."))

    def test_exact_locator_and_hash_pass_technical_check(self) -> None:
        self.assertTrue(verify_parser_run(self.doc, self.parser_run))
        self.assertEqual(verify_evidence_ref(self.snapshot, self.doc, self.parser_run, self.ref),
                         CitationCheck.PASS_TECHNICAL)

    def test_wrong_text_parser_document_or_snapshot_is_rejected(self) -> None:
        altered_run = create_parser_run(self.doc, parser_version="fixture-parser-v1",
                                        normalized_text=self.text + " changed")
        self.assertEqual(verify_evidence_ref(self.snapshot, self.doc, altered_run, self.ref), CitationCheck.REJECT)
        revised_doc = document(body=b"different source", native_id="other-doc")
        self.assertEqual(verify_evidence_ref(self.snapshot, revised_doc, self.parser_run, self.ref),
                         CitationCheck.REJECT)
        later_snapshot = build_snapshot([self.doc], as_of=T0 + timedelta(days=1))
        self.assertEqual(verify_evidence_ref(later_snapshot, self.doc, self.parser_run, self.ref),
                         CitationCheck.REJECT)

    def test_parser_run_and_evidence_are_bound_to_document_snapshot_and_locator(self) -> None:
        with self.assertRaisesRegex(ResearchEvidenceError, "outside the normalized text"):
            create_evidence_ref(self.snapshot, self.doc, parser_run=self.parser_run, start=-1, end=2)
        altered_doc_metadata = replace(self.doc, license_status=LicenseStatus.DENIED)
        self.assertEqual(altered_doc_metadata.document_version_id, self.doc.document_version_id)
        self.assertEqual(verify_evidence_ref(self.snapshot, altered_doc_metadata,
                                             self.parser_run, self.ref), CitationCheck.REJECT)
        forged_ref = replace(self.ref, locator_start=1)
        self.assertEqual(verify_evidence_ref(self.snapshot, self.doc, self.parser_run, forged_ref),
                         CitationCheck.REJECT)

    def test_evidence_id_changes_with_parser_or_snapshot(self) -> None:
        other_parser = create_parser_run(self.doc, parser_version="fixture-parser-v2",
                                         normalized_text=self.text)
        other_ref = create_evidence_ref(self.snapshot, self.doc, parser_run=other_parser,
                                        start=0, end=len("Synthetic revenue: 120 units."))
        self.assertNotEqual(self.ref.evidence_id, other_ref.evidence_id)

    def test_forged_parser_or_malformed_evidence_fails_closed(self) -> None:
        forged_parser = replace(self.parser_run, parser_run_id="rd-parser-v1-forged")
        self.assertFalse(verify_parser_run(self.doc, forged_parser))
        self.assertEqual(verify_evidence_ref(self.snapshot, self.doc, forged_parser, self.ref),
                         CitationCheck.REJECT)
        malformed_ref = replace(self.ref, quoted_text_sha256=[])  # type: ignore[arg-type]
        self.assertEqual(verify_evidence_ref(self.snapshot, self.doc, self.parser_run, malformed_ref),
                         CitationCheck.REJECT)


class IsolationTests(unittest.TestCase):
    def test_offline_contract_does_not_open_network_or_import_runtime_execution_modules(self) -> None:
        package = Path(__file__).resolve().parents[2] / "src" / "invest_ai" / "research"
        forbidden = {"socket", "urllib", "http", "subprocess", "execution", "integration",
                     "policy", "risk", "portfolio", "core"}
        for source in package.glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".", 1)[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots = {node.module.split(".", 1)[0]}
                else:
                    continue
                self.assertFalse(roots & forbidden, f"forbidden import in {source}: {roots & forbidden}")
                if source.name == "evidence.py":
                    self.assertFalse(roots & {"sqlite3", "pathlib"},
                                     f"evidence model acquired storage capability: {roots & {'sqlite3', 'pathlib'}!r}")
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            item = document()
            self.assertTrue(build_snapshot([item], as_of=T0).snapshot_id)


if __name__ == "__main__":
    unittest.main()
