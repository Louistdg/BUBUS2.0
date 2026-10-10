"""Independent adversarial checks for the offline-only SEC fixture prototype.

All values are synthetic; this suite must never open a socket or read Runtime.
Known missing binding controls are written as failing security assertions so
the audit records the defect instead of blessing the current behavior.
"""
from __future__ import annotations

import ast
import hashlib
import json
import socket
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from invest_ai.data.sec_offline_fixture import (
    FixtureEvidenceLedger,
    LicenseStatus,
    PitStatus,
    ResponseDisposition,
    SecFixtureError,
    SecFixtureRequest,
    SimulatedSharedQuota,
    SyntheticHttpResponse,
    eligible_as_of,
    process_fixture,
    verify_response_fixture,
)


UTC = timezone.utc
T0 = datetime(2026, 10, 8, 12, tzinfo=UTC)
BODY = b"synthetic SEC filing body"
REQUEST = SecFixtureRequest(
    dataset="filing_document", cik="0000320193", form="10-K",
    accession="0000320193-26-000001", document_name="primary.htm",
)


def make_response(*, status: int = 200, body: bytes = BODY, at: datetime = T0,
                  headers: dict[str, str] | None = None) -> SyntheticHttpResponse:
    values = {"content-type": "text/html", "content-length": str(len(body))}
    values.update(headers or {})
    return SyntheticHttpResponse(status, values, body, at)


def make_evidence(request: SecFixtureRequest = REQUEST, *, body: bytes = BODY,
                  at: datetime = T0, form_date: date = date(2026, 10, 7),
                  license_status: LicenseStatus = LicenseStatus.UNKNOWN,
                  supersedes_id: str | None = None):
    return process_fixture(
        request=request, filing_date=form_date, accepted_at=None,
        response=make_response(body=body, at=at),
        license_id="SYNTHETIC_FIXTURE_LICENSE", license_status=license_status,
        supersedes_id=supersedes_id,
    )


class SecOfflineFixtureIndependentReaudit01(unittest.TestCase):
    def test_accession_cik_mismatch_is_rejected(self) -> None:
        # Syntactically valid accession naming a different filer must not bind
        # evidence to the caller-selected CIK.
        with self.assertRaises(SecFixtureError):
            SecFixtureRequest(
                dataset="filing_document", cik="0000320193", form="10-K",
                accession="0000789019-26-000001", document_name="primary.htm",
            )

    def test_same_accession_identity_conflict_is_not_deduplicated(self) -> None:
        ledger = FixtureEvidenceLedger()
        original = make_evidence()
        ledger.append(original)
        conflicting_request = replace(REQUEST, form="10-Q", document_name="different.htm")
        conflicting = make_evidence(conflicting_request)
        with self.assertRaises(SecFixtureError):
            ledger.append(conflicting)

    def test_manifest_tampering_is_detected_without_recomputing_hash(self) -> None:
        item = make_evidence()
        altered = replace(item, first_observed_available_at=T0 - timedelta(days=1))
        self.assertFalse(altered.verify())
        self.assertFalse(eligible_as_of(altered, T0))

    def test_license_allowlist_value_is_only_a_caller_supplied_fixture_assertion(self) -> None:
        # This proves the helper has no authority boundary: the bool-like enum
        # is part of caller-built evidence. ALLOWED is synthetic test metadata,
        # never a real SEC license decision.
        item = make_evidence(license_status=LicenseStatus.ALLOWED)
        self.assertTrue(item.verify())
        self.assertTrue(eligible_as_of(item, T0))
        self.assertNotIn("signature", item.manifest())

    def test_unkeyed_manifest_can_be_rebuilt_by_the_caller(self) -> None:
        # A digest detects accidental edits, but it cannot authenticate the
        # manifest when the caller can recompute both capture and manifest IDs.
        item = make_evidence()
        fields = item.manifest()
        fields.pop("manifest_sha256")
        fields["license_status"] = LicenseStatus.ALLOWED.value
        fields["capture_id"] = ""
        canonical = lambda value: json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        capture_id = "sec-capture-" + hashlib.sha256(canonical(fields)).hexdigest()
        fields["capture_id"] = capture_id
        manifest_sha = hashlib.sha256(canonical(fields)).hexdigest()
        forged = replace(
            item, license_status=LicenseStatus.ALLOWED,
            capture_id=capture_id, manifest_sha256=manifest_sha,
        )
        self.assertTrue(forged.verify())
        self.assertTrue(eligible_as_of(forged, T0))

    def test_pit_uses_first_observation_not_filing_or_acceptance_time(self) -> None:
        old_filing = make_evidence(form_date=date(2010, 1, 1), at=T0)
        self.assertEqual(old_filing.pit_status, PitStatus.FIRST_OBSERVED_CONSERVATIVE)
        self.assertIsNone(old_filing.public_available_at)
        self.assertFalse(eligible_as_of(old_filing, T0 - timedelta(days=1)))
        self.assertFalse(eligible_as_of(old_filing, datetime(2010, 1, 2, tzinfo=UTC)))

    def test_process_fixture_cutoff_is_not_an_eligibility_result(self) -> None:
        future = T0 + timedelta(days=1)
        item = process_fixture(
            request=REQUEST, filing_date=date(2026, 10, 7), accepted_at=None,
            response=make_response(at=future), license_id="SYNTHETIC_FIXTURE_LICENSE",
            license_status=LicenseStatus.ALLOWED, as_of=T0,
        )
        # The API intentionally leaves cutoff evaluation to eligible_as_of;
        # consumers must not infer eligibility from successful construction.
        self.assertEqual(item.available_at, future)
        self.assertFalse(eligible_as_of(item, T0))

    def test_utc_equivalent_cutoff_and_naive_cutoff_handling(self) -> None:
        item = make_evidence(license_status=LicenseStatus.ALLOWED)
        equivalent = datetime(2026, 10, 8, 14, tzinfo=timezone(timedelta(hours=2)))
        self.assertTrue(eligible_as_of(item, equivalent))
        with self.assertRaises((SecFixtureError, ValueError)):
            eligible_as_of(item, datetime(2026, 10, 8, 12))

    def test_3xx_403_429_mime_and_length_fail_closed(self) -> None:
        cases = (
            (make_response(status=302, headers={"location": "http://127.0.0.1/admin"}),
             ResponseDisposition.REDIRECT_REJECTED),
            (make_response(status=403), ResponseDisposition.REJECTED),
            (make_response(status=429, headers={"retry-after": "20"}),
             ResponseDisposition.RATE_LIMITED),
            (make_response(headers={"content-type": "application/json"}),
             ResponseDisposition.REJECTED),
            (make_response(headers={"content-length": "999"}), ResponseDisposition.REJECTED),
            (make_response(status=429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
             ResponseDisposition.RATE_LIMITED),
        )
        for response, expected in cases:
            with self.subTest(status=response.status, headers=dict(response.headers)):
                self.assertEqual(verify_response_fixture(response, "filing_document"), expected)

    def test_numeric_retry_after_is_recorded_but_no_wait_or_retry_occurs(self) -> None:
        item = process_fixture(
            request=REQUEST, filing_date=date(2026, 10, 7), accepted_at=None,
            response=make_response(status=429, headers={"retry-after": "20"}),
            license_id="SYNTHETIC_FIXTURE_LICENSE",
        )
        self.assertEqual(item.retry_after_seconds, 20)
        self.assertIsNone(item.available_at)

    def test_simulated_quota_is_deterministic_for_sequential_callers(self) -> None:
        quota = SimulatedSharedQuota()
        self.assertTrue(quota.request(T0).allowed)
        self.assertFalse(quota.request(T0 + timedelta(milliseconds=999)).allowed)
        self.assertTrue(quota.request(T0 + timedelta(seconds=1)).allowed)

    def test_module_import_and_processing_do_not_open_network(self) -> None:
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            self.assertTrue(make_evidence().verify())

    def test_module_has_no_direct_network_or_financial_imports(self) -> None:
        path = ROOT / "src" / "invest_ai" / "data" / "sec_offline_fixture.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {
            "socket", "urllib", "http", "requests", "httpx", "risk", "gateway",
            "execution", "integration", "policy", "portfolio",
        })


if __name__ == "__main__":
    unittest.main()
