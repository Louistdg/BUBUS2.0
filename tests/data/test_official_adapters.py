from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from invest_ai.data.models import DataContractError, Filing, FundamentalFact, MacroObservation
from invest_ai.data.official import (
    EcbAdapter, GleifAdapter, HttpResponse, LicenseBlocked, OfficialDataIngestor,
    OfficialRawArchive, OfficialRawEnvelope, OfficialAdapterError, SecAdapter,
    SourceRegistry, normalize_ecb_csv, normalize_gleif, normalize_sec_companyfacts,
    normalize_sec_submissions,
)
from invest_ai.data.pipeline import DataIngestor
from invest_ai.data.providers import ProviderRequest


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "data" / "fixtures"
T0 = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: list[HttpResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def get(self, url: str, *, params, headers) -> HttpResponse:
        self.calls.append((url, dict(params), dict(headers)))
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def response(path: str, content_type: str = "application/json") -> HttpResponse:
    return HttpResponse((FIXTURES / path).read_bytes(), {"Content-Type": content_type, "ETag": '"fixture"'})


class OfficialAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = SourceRegistry.load()

    def test_registry_matches_lead_decision_and_fails_closed(self) -> None:
        self.assertEqual(self.registry.require("gleif").status, "GREEN")
        self.assertEqual(self.registry.require("sec").status, "GREEN")
        self.assertEqual(self.registry.require("ecb").status, "GREEN")
        for source in ("fred", "alfred", "nbb", "eurostat", "esap"):
            with self.assertRaises(LicenseBlocked):
                self.registry.require(source)
        with self.assertRaises(LicenseBlocked):
            self.registry.require("ecb", third_party_data=True)

    def test_gleif_normalizes_entity_and_archives_exact_body_and_manifest(self) -> None:
        body = (FIXTURES / "official_gleif_lei.json").read_bytes()
        transport = FakeTransport([HttpResponse(body, {"Content-Type": "application/vnd.api+json", "ETag": "x"})])
        adapter = GleifAdapter(transport=transport, registry=self.registry, clock=lambda: T0)
        request = ProviderRequest(datasets=("entity_reference",), instrument_ids=("5493001KJTIIGC8Y1R12",))
        with tempfile.TemporaryDirectory() as folder:
            outcomes = OfficialDataIngestor(folder).ingest(
                adapter, request, {"entity_reference": lambda envelope, archived: (normalize_gleif(envelope, archived),)},
            )
            self.assertEqual(len(outcomes), 1)
            raw = Path(folder, *Path(outcomes[0].raw_reference).parts)
            self.assertEqual(raw.read_bytes(), body)
            self.assertEqual(outcomes[0].raw_sha256, hashlib.sha256(body).hexdigest())
            manifest = Path(folder, *Path(outcomes[0].manifest_reference).parts)
            item = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(item["body_sha256"], outcomes[0].raw_sha256)
            self.assertEqual(hashlib.sha256(manifest.read_bytes()).hexdigest(), outcomes[0].manifest_sha256)
            self.assertEqual(item["terms_id"], "GLEIF_LEI_CC0_1_0")
            self.assertEqual(item["response_headers"]["ETag"], "x")
            self.assertNotIn("Authorization", item["request_headers"])
            self.assertNotIn("api_key", item["request_parameters"])
            self.assertNotIn("Set-Cookie", item["response_headers"])
            stored = list((Path(folder) / "data" / "normalized").rglob("*.json"))
            self.assertEqual(len(stored), 1)
            normalized = json.loads(stored[0].read_text(encoding="utf-8"))
            self.assertEqual(normalized["lei"], "5493001KJTIIGC8Y1R12")
            self.assertEqual(normalized["legal_name"], "Example Holdings PLC")
            self.assertEqual(transport.calls[0][0], "https://api.gleif.org/api/v1/lei-records/5493001KJTIIGC8Y1R12")

    def test_exact_duplicate_gleif_capture_is_idempotent(self) -> None:
        transport = FakeTransport([response("official_gleif_lei.json", "application/vnd.api+json")] * 2)
        adapter = GleifAdapter(transport=transport, registry=self.registry, clock=lambda: T0)
        request = ProviderRequest(datasets=("entity_reference",), instrument_ids=("5493001KJTIIGC8Y1R12",))
        with tempfile.TemporaryDirectory() as folder:
            ingestor = OfficialDataIngestor(folder)
            normalizers = {"entity_reference": lambda envelope, archived: (normalize_gleif(envelope, archived),)}
            first = ingestor.ingest(adapter, request, normalizers)[0]
            second = ingestor.ingest(adapter, request, normalizers)[0]
            self.assertEqual(first.raw_reference, second.raw_reference)
            self.assertEqual(first.manifest_reference, second.manifest_reference)
            self.assertEqual(first.normalized[0].status, "APPENDED")
            self.assertEqual(second.normalized[0].status, "DUPLICATE")

    def test_unchanged_official_record_is_not_revised_when_capture_time_changes(self) -> None:
        transport = FakeTransport([response("official_gleif_lei.json", "application/vnd.api+json")] * 2)
        clock_values = iter((T0, T0, T0.replace(minute=1), T0.replace(minute=1)))
        adapter = GleifAdapter(transport=transport, registry=self.registry,
                               clock=lambda: next(clock_values))
        request = ProviderRequest(datasets=("entity_reference",), instrument_ids=("5493001KJTIIGC8Y1R12",))
        with tempfile.TemporaryDirectory() as folder:
            ingestor = OfficialDataIngestor(folder)
            normalizers = {"entity_reference": lambda envelope, archived: (normalize_gleif(envelope, archived),)}
            first = ingestor.ingest(adapter, request, normalizers)[0]
            second = ingestor.ingest(adapter, request, normalizers)[0]
            self.assertNotEqual(first.manifest_reference, second.manifest_reference)
            self.assertEqual(second.normalized[0].status, "DUPLICATE")
            normalized_files = list((Path(folder) / "data" / "normalized").rglob("*.json"))
            self.assertEqual(len(normalized_files), 1)

    def test_sec_without_explicit_contact_fails_before_network(self) -> None:
        transport = FakeTransport([response("official_sec_submissions.json")])
        adapter = SecAdapter(contact=None, transport=transport, registry=self.registry, clock=lambda: T0)
        with self.assertRaisesRegex(OfficialAdapterError, "explicitly configured contact"):
            list(adapter.fetch_envelopes(ProviderRequest(datasets=("submissions",), instrument_ids=("9999999999",))))
        self.assertEqual(transport.calls, [])

    def test_generic_ingestor_cannot_drop_official_source_manifest(self) -> None:
        transport = FakeTransport()
        adapter = SecAdapter(contact="operator@example.invalid", transport=transport, registry=self.registry)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(DataContractError, "OfficialDataIngestor"):
                DataIngestor(folder).ingest(
                    adapter,
                    ProviderRequest(datasets=("submissions",), instrument_ids=("9999999999",)),
                    {},
                )
        self.assertEqual(transport.calls, [])

    def test_sec_declares_configured_user_agent_and_preserves_amendments(self) -> None:
        body = (FIXTURES / "official_sec_submissions.json").read_bytes()
        transport = FakeTransport([HttpResponse(body, {"Content-Type": "application/json"})])
        adapter = SecAdapter(contact="operator@example.invalid", transport=transport,
                             registry=self.registry, clock=lambda: T0)
        envelope = next(adapter.fetch_envelopes(
            ProviderRequest(datasets=("submissions",), instrument_ids=("9999999999",))
        ))
        self.assertIn("operator@example.invalid", transport.calls[0][2]["User-Agent"])
        self.assertEqual(envelope.payload.body, body)
        with tempfile.TemporaryDirectory() as folder:
            archived, _, _ = OfficialRawArchive(folder).archive(envelope)
            records = normalize_sec_submissions(envelope, archived)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(isinstance(item, Filing) for item in records))
        self.assertEqual(records[1].form, "10-Q/A")
        self.assertEqual(records[0].accession, "9999999999-26-000101")
        self.assertIsNone(records[0].provenance.published_at)
        self.assertEqual(records[0].accepted_at.isoformat(), "2026-10-06T21:57:59+00:00")
        self.assertIn("/q3.htm", records[0].document_url)

    def test_sec_companyfacts_normalizes_decimal_fact_and_keeps_accession(self) -> None:
        transport = FakeTransport([response("official_sec_companyfacts.json")])
        adapter = SecAdapter(contact="operator@example.invalid", transport=transport,
                             registry=self.registry, clock=lambda: T0)
        envelope = next(adapter.fetch_envelopes(
            ProviderRequest(datasets=("companyfacts",), instrument_ids=("9999999999",))
        ))
        with tempfile.TemporaryDirectory() as folder:
            archived, _, _ = OfficialRawArchive(folder).archive(envelope)
            facts = normalize_sec_companyfacts(envelope, archived)
        self.assertEqual(len(facts), 1)
        self.assertIsInstance(facts[0], FundamentalFact)
        self.assertEqual(facts[0].value, Decimal("123456.75"))
        self.assertEqual(facts[0].accession, "9999999999-26-000101")
        self.assertEqual(facts[0].xbrl_tag, "RevenueFromContractWithCustomerExcludingAssessedTax")
        self.assertEqual(facts[0].provenance.unit, "USD")
        self.assertIsNone(facts[0].provenance.published_at)

    def test_sec_invalid_contact_is_rejected_before_network(self) -> None:
        transport = FakeTransport([response("official_sec_submissions.json")])
        adapter = SecAdapter(contact="not-a-contact", transport=transport, registry=self.registry)
        with self.assertRaisesRegex(OfficialAdapterError, "explicitly configured contact"):
            list(adapter.fetch_envelopes(ProviderRequest(datasets=("submissions",), instrument_ids=("9999999999",))))
        self.assertEqual(transport.calls, [])

    def test_sec_filing_document_rejects_path_traversal(self) -> None:
        transport = FakeTransport()
        adapter = SecAdapter(contact="operator@example.invalid", transport=transport, registry=self.registry)
        request = ProviderRequest(
            datasets=("filing_document",),
            instrument_ids=("9999999999:999999999926000101:../secret.htm",),
        )
        with self.assertRaisesRegex(DataContractError, "document name"):
            list(adapter.fetch_envelopes(request))
        self.assertEqual(transport.calls, [])

    def test_sec_filing_document_is_get_only_and_bound_to_accession(self) -> None:
        body = b"<html>synthetic filing document</html>"
        transport = FakeTransport([HttpResponse(body, {"Content-Type": "text/html"})])
        adapter = SecAdapter(contact="operator@example.invalid", transport=transport,
                             registry=self.registry, clock=lambda: T0)
        envelope = next(adapter.fetch_envelopes(ProviderRequest(
            datasets=("filing_document",),
            instrument_ids=("9999999999:9999999999-26-000101:q3.htm",),
        )))
        self.assertEqual(transport.calls[0][0],
                         "https://www.sec.gov/Archives/edgar/data/9999999999/999999999926000101/q3.htm")
        self.assertEqual(envelope.payload.body, body)
        self.assertEqual(envelope.payload.dataset, "filing_document")

    def test_ecb_requires_allowlist_and_does_not_invent_published_at(self) -> None:
        spec = "EXR:D.USD.EUR.SP00.A"
        transport = FakeTransport([response("official_ecb_series.csv", "text/csv")])
        blocked = EcbAdapter(approved_series=(), transport=transport, registry=self.registry)
        with self.assertRaises(LicenseBlocked):
            list(blocked.fetch_envelopes(ProviderRequest(datasets=(spec,))))
        self.assertEqual(transport.calls, [])

        adapter = EcbAdapter(approved_series=(spec,), transport=transport,
                             registry=self.registry, clock=lambda: T0, include_history=True)
        envelope = next(adapter.fetch_envelopes(ProviderRequest(datasets=(spec,))))
        self.assertEqual(transport.calls[0][1]["includeHistory"], "true")
        with tempfile.TemporaryDirectory() as folder:
            archived, _, _ = OfficialRawArchive(folder).archive(envelope)
            rows = normalize_ecb_csv(envelope, archived)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(isinstance(item, MacroObservation) for item in rows))
        self.assertEqual(rows[0].value, Decimal("1.1567"))
        self.assertEqual(rows[0].period, "2026-10-05")
        self.assertEqual(rows[0].provenance.unit, "USD/EUR")
        self.assertIsNone(rows[0].provenance.published_at)
        self.assertEqual(rows[0].provenance.observed_at, T0)
        self.assertIsNone(rows[2].value)
        self.assertIn("status=M", rows[2].missing_reason)

    def test_raw_revision_payloads_are_append_only_and_distinct(self) -> None:
        raw1 = b'{"value":"1.0"}'
        raw2 = b'{"value":"1.1"}'
        def envelope(body: bytes, revision: int) -> OfficialRawEnvelope:
            from invest_ai.data.providers import RawPayload
            payload = RawPayload(
                provider="gleif", dataset="entity_reference", source="https://fixture.invalid/lei",
                record_id="5493001KJTIIGC8Y1R12", body=body, observed_at=T0,
                received_at=T0, available_at=T0, revision=revision, media_type="application/json",
            )
            return OfficialRawEnvelope(
                payload=payload, request_url=payload.source, request_parameters={},
                request_headers={}, response_headers={}, source_schema_version="fixture-v1",
                terms_id="GLEIF_LEI_CC0_1_0",
            )
        with tempfile.TemporaryDirectory() as folder:
            archive = OfficialRawArchive(folder)
            first, _, _ = archive.archive(envelope(raw1, 1))
            second, _, _ = archive.archive(envelope(raw2, 2))
            self.assertNotEqual(first.reference, second.reference)
            self.assertEqual(Path(folder, *Path(first.reference).parts).read_bytes(), raw1)
            self.assertEqual(Path(folder, *Path(second.reference).parts).read_bytes(), raw2)
            self.assertEqual(len(list((Path(folder) / "data" / "raw_manifests").rglob("*.json"))), 2)

    def test_corrected_ecb_value_is_appended_as_local_revision(self) -> None:
        spec = "EXR:D.USD.EUR.SP00.A"
        first_body = b"KEY,FREQ,CURRENCY,DENOM,TIME_PERIOD,OBS_VALUE,OBS_STATUS\nD.USD.EUR.SP00.A,D,USD,EUR,2026-10-06,1.10,A\n"
        corrected_body = first_body.replace(b"1.10", b"1.11")
        transport = FakeTransport([
            HttpResponse(first_body, {"Content-Type": "text/csv"}),
            HttpResponse(corrected_body, {"Content-Type": "text/csv"}),
        ])
        adapter = EcbAdapter(transport=transport, registry=self.registry, clock=lambda: T0)
        normalizers = {"macro_observation": normalize_ecb_csv}
        request = ProviderRequest(datasets=(spec,))
        with tempfile.TemporaryDirectory() as folder:
            ingestor = OfficialDataIngestor(folder)
            first = ingestor.ingest(adapter, request, normalizers)[0]
            second = ingestor.ingest(adapter, request, normalizers)[0]
            self.assertEqual(first.normalized[0].status, "APPENDED")
            self.assertEqual(second.normalized[0].status, "APPENDED")
            files = list((Path(folder) / "data" / "normalized" / "ecb" / "macro_observation").glob("*.json"))
            self.assertEqual(len(files), 2)
            records = [json.loads(path.read_text(encoding="utf-8")) for path in files]
            self.assertEqual(sorted(item["provenance"]["revision"] for item in records), [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
