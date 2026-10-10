from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path

from invest_ai.railway_synthetic.api import TestAuthenticator, make_server
from invest_ai.railway_synthetic.contract import ContractError, build_snapshot, canonical_json, parse_json_no_duplicates, validate_snapshot
from invest_ai.railway_synthetic.fixtures import make_first_snapshot, synthetic_payload
from invest_ai.railway_synthetic.store import IngestConflict, SyntheticProjectionStore


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def next_snapshot(previous: dict, *, sequence: int | None = None, payload: dict | None = None) -> dict:
    return build_snapshot(
        sequence=sequence or previous["sequence"] + 1,
        previous_snapshot_hash=previous["snapshot_sha256"],
        created_at_utc=f"2026-10-09T12:00:{(sequence or previous['sequence'] + 1):02d}.000Z",
        payload=payload or synthetic_payload(),
    )


class SyntheticPrototypeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="invest-ai-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "projection-test.db"
        self.clock = Clock()
        self.store = SyntheticProjectionStore(self.path, clock=self.clock)

    def test_closed_schema_provenance_and_no_operational_or_financial_fields(self):
        snapshot = make_first_snapshot()
        self.assertEqual(validate_snapshot(snapshot), snapshot)
        raw = canonical_json(snapshot)
        self.assertIn(b"SYNTHETIC_ONLY", raw)
        self.assertNotIn(b"may_start", raw)
        self.assertNotIn(b"authorized", raw)
        self.assertNotIn(b"balance", raw.lower())
        self.assertNotIn(b"order", raw.lower())
        bad = json.loads(raw)
        bad["payload"]["agents"][0]["synthetic_only"] = False
        with self.assertRaises(ContractError):
            build_snapshot(sequence=1, previous_snapshot_hash=None, created_at_utc="2026-10-09T12:00:00.000Z", payload=bad["payload"])
        bad = json.loads(raw)
        bad["operational_green"] = True
        with self.assertRaises(ContractError):
            validate_snapshot(bad)

    def test_arbitrary_free_text_is_not_accepted_as_synthetic_provenance(self):
        snapshot = make_first_snapshot()
        bad_payload = json.loads(json.dumps(snapshot["payload"]))
        bad_payload["missions"][0]["title"] = "SYNTHETIC-CHECK-UNVERIFIED-FREE-TEXT-987654"
        with self.assertRaises(ContractError):
            build_snapshot(sequence=1, previous_snapshot_hash=None, created_at_utc=snapshot["created_at_utc"], payload=bad_payload)
        bad_payload = json.loads(json.dumps(snapshot["payload"]))
        bad_payload["agents"][0]["display_name"] = "arbitrary alleged synthetic prose"
        with self.assertRaises(ContractError):
            build_snapshot(sequence=1, previous_snapshot_hash=None, created_at_utc=snapshot["created_at_utc"], payload=bad_payload)

    def test_duplicate_keys_hash_tamper_and_noncanonical_values_rejected(self):
        with self.assertRaises(ContractError):
            parse_json_no_duplicates(b'{"x":1,"x":2}')
        snapshot = make_first_snapshot()
        altered = json.loads(json.dumps(snapshot))
        altered["payload"]["demo_summary"]["simulated_state"] = "SIMULATED_GREEN"
        with self.assertRaises(ContractError):
            validate_snapshot(altered)
        altered = json.loads(json.dumps(snapshot))
        altered["sequence"] = 1.0
        with self.assertRaises(ContractError):
            validate_snapshot(altered)

    def test_ingest_retry_is_idempotent_and_does_not_refresh_receive_time(self):
        snapshot = make_first_snapshot()
        first = self.store.ingest(snapshot)
        self.clock.value += timedelta(seconds=30)
        duplicate = self.store.ingest(snapshot)
        self.assertEqual(first["result"], "ACCEPTED")
        self.assertEqual(duplicate["result"], "IDEMPOTENT")
        self.assertEqual(first["received_at_utc"], duplicate["received_at_utc"])

    def test_sequence_gap_and_wrong_parent_fail_closed(self):
        first = make_first_snapshot()
        self.store.ingest(first)
        gap = next_snapshot(first, sequence=3)
        with self.assertRaises(IngestConflict) as caught:
            self.store.ingest(gap)
        self.assertEqual(caught.exception.code, "SEQUENCE_GAP")
        wrong_parent = build_snapshot(sequence=2, previous_snapshot_hash="f" * 64, created_at_utc="2026-10-09T12:00:02.000Z", payload=synthetic_payload())
        with self.assertRaises(IngestConflict) as caught:
            self.store.ingest(wrong_parent)
        self.assertEqual(caught.exception.code, "HEAD_MISMATCH")
        self.assertEqual(self.store.snapshot(), first)

    def test_same_sequence_different_valid_content_marks_corrupt(self):
        first = make_first_snapshot()
        self.store.ingest(first)
        changed_payload = synthetic_payload()
        changed_payload["demo_summary"]["simulated_state"] = "SIMULATED_RED"
        conflict = build_snapshot(sequence=1, previous_snapshot_hash=None, created_at_utc="2026-10-09T12:00:00.000Z", payload=changed_payload)
        with self.assertRaises(IngestConflict) as caught:
            self.store.ingest(conflict)
        self.assertTrue(caught.exception.corrupt)
        self.assertEqual(self.store.health()["sync_state"], "UNKNOWN")

    def test_commit_failure_rolls_back_snapshot_and_head(self):
        failing = SyntheticProjectionStore(self.path, clock=self.clock, fail_before_commit=True)
        with self.assertRaises(sqlite3.OperationalError):
            failing.ingest(make_first_snapshot())
        self.assertIsNone(self.store.snapshot())
        self.assertEqual(self.store.health()["sync_state"], "UNKNOWN")

    def test_reopen_and_handshake_required_then_120_second_freshness(self):
        first = make_first_snapshot()
        self.store.ingest(first)
        reopened = SyntheticProjectionStore(self.path, clock=self.clock)
        self.assertEqual(reopened.health()["sync_state"], "FRESH")
        self.assertEqual(reopened.handshake(first["source_instance_id"], 1, first["snapshot_sha256"]), "MATCH")
        self.clock.value += timedelta(seconds=120)
        self.assertEqual(reopened.health()["sync_state"], "FRESH")
        self.clock.value += timedelta(seconds=1)
        self.assertEqual(reopened.health()["sync_state"], "STALE")

    def test_exact_fractional_ttl_boundaries(self):
        first = make_first_snapshot()
        self.store.ingest(first)
        received = self.clock.value
        cases = [(119, "FRESH"), (120, "FRESH"), (120.001, "STALE"), (120.999, "STALE"), (121, "STALE")]
        for age, expected in cases:
            with self.subTest(age_seconds=age):
                now = received + timedelta(seconds=age)
                result = self.store.health(now=now)
                self.assertEqual(result["sync_state"], expected)

    def test_handshake_divergence_does_not_refresh_freshness(self):
        first = make_first_snapshot()
        accepted = self.store.ingest(first)
        self.assertEqual(self.store.handshake(first["source_instance_id"], 2, first["snapshot_sha256"]), "SOURCE_AHEAD")
        self.assertEqual(self.store.health()["sync_state"], "UNKNOWN")
        self.assertEqual(self.store.handshake(first["source_instance_id"], 1, first["snapshot_sha256"]), "MATCH")
        self.assertEqual(self.store.health()["received_at_utc"], accepted["received_at_utc"])

    def test_concurrent_same_snapshot_ingestion_is_idempotent(self):
        snapshot = make_first_snapshot()
        results: list[str] = []
        failures: list[Exception] = []

        def run():
            try:
                results.append(self.store.ingest(snapshot)["result"])
            except Exception as exc:  # test captures race failures for assertion
                failures.append(exc)

        threads = [threading.Thread(target=run) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(failures)
        self.assertEqual(results.count("ACCEPTED"), 1)
        self.assertEqual(results.count("IDEMPOTENT"), 5)

    def test_replay_chain_and_persisted_head(self):
        first = make_first_snapshot()
        self.store.ingest(first)
        second = next_snapshot(first)
        self.store.ingest(second)
        reopened = SyntheticProjectionStore(self.path, clock=self.clock)
        self.assertEqual(reopened.snapshot(), second)
        self.assertEqual(reopened.handshake(second["source_instance_id"], 2, second["snapshot_sha256"]), "MATCH")

    def test_storage_path_must_be_explicitly_temporary(self):
        with self.assertRaises(ValueError):
            SyntheticProjectionStore(Path.cwd() / "must-not-create.db", clock=self.clock)


class SyntheticApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="invest-ai-synthetic-api-")
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.store = SyntheticProjectionStore(Path(self.temp.name) / "api.db", clock=self.clock)
        self.snapshot = make_first_snapshot()
        self.auth = TestAuthenticator(viewer_tokens={"viewer-demo"}, publisher_tokens={"publisher-demo"}, recognized_tokens={"recognized-no-viewer-role"})
        self.server = make_server(self.store, self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, *, token: str | None = None, body: dict | None = None):
        connection = HTTPConnection(*self.server.server_address, timeout=3)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            encoded = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        else:
            encoded = None
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_authentication_401_and_wrong_audience_401(self):
        self.assertEqual(self.request("GET", "/api/v1/synthetic/health")[0], 401)
        self.assertEqual(self.request("GET", "/api/v1/synthetic/health", token="publisher-demo")[0], 401)
        self.assertEqual(self.request("GET", "/api/v1/synthetic/health", token="recognized-no-viewer-role")[0], 403)

    def test_server_refuses_non_loopback_bind(self):
        for host in ("0.0.0.0", "192.0.2.10", "::"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                make_server(self.store, self.auth, host=host)

    def test_ipv6_loopback_is_explicitly_refused_by_ipv4_server(self):
        with self.assertRaisesRegex(ValueError, "IPv6 loopback is unsupported"):
            make_server(self.store, self.auth, host="::1")

    def test_dashboard_has_no_bundled_token_and_refreshes_within_ttl(self):
        root = Path(__file__).resolve().parents[2]
        dashboard = (root / "src" / "invest_ai" / "railway_synthetic" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("SYNTHETIC ONLY / SIMULATION", dashboard)
        self.assertIn("window.setInterval(refreshSyntheticDashboard, 30000)", dashboard)
        self.assertIn("credentials: 'same-origin'", dashboard)
        self.assertNotIn("SYNTHETIC_VIEWER_TOKEN", dashboard)
        self.assertIn("data.sync_state !== 'FRESH'", dashboard)
        self.assertIn("h.sync_state !== 'FRESH'", dashboard)
        self.assertIn("UNAVAILABLE", dashboard)

    def test_ingestion_auth_and_get_view(self):
        self.assertEqual(self.request("POST", "/internal/v1/synthetic/snapshots", body=self.snapshot)[0], 401)
        self.assertEqual(self.request("POST", "/internal/v1/synthetic/snapshots", token="publisher-demo", body=self.snapshot)[0], 201)
        status, headers, body = self.request("GET", "/api/v1/synthetic/health", token="viewer-demo")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(data["dataset_mode"], "SYNTHETIC_ONLY")
        self.assertNotIn("may_start", data)
        status, _, body = self.request("GET", "/api/v1/synthetic/missions", token="viewer-demo")
        self.assertEqual(status, 200)
        self.assertTrue(all(mission["mission_id"].startswith("SYN-") for mission in json.loads(body)["items"]))

    def test_methods_refused_and_no_remote_mutations(self):
        for method in ("PUT", "PATCH", "DELETE", "OPTIONS"):
            self.assertEqual(self.request(method, "/api/v1/synthetic/missions", token="viewer-demo")[0], 405)
            self.assertEqual(self.request(method, "/internal/v1/synthetic/snapshots", token="publisher-demo")[0], 405)
        for path in ("/api/v1/synthetic/missions", "/api/v1/synthetic/claims", "/api/v1/synthetic/decisions"):
            self.assertEqual(self.request("POST", path, token="publisher-demo", body={})[0], 405)

    def test_invalid_content_type_size_and_schema_rejected(self):
        connection = HTTPConnection(*self.server.server_address, timeout=3)
        connection.request("POST", "/internal/v1/synthetic/snapshots", body=b"{}", headers={"Authorization": "Bearer publisher-demo", "Content-Type": "text/plain", "Content-Length": "2"})
        self.assertEqual(connection.getresponse().status, 415)
        connection.close()
        bad = json.loads(json.dumps(self.snapshot))
        bad["payload"]["missions"][0]["mission_id"] = "REAL-MISSION-1"
        self.assertEqual(self.request("POST", "/internal/v1/synthetic/snapshots", token="publisher-demo", body=bad)[0], 422)

    def test_d1_old_snapshot_and_freshness_never_cross_sequence(self):
        self.store.ingest(self.snapshot)
        second = next_snapshot(self.snapshot)
        self.clock.value += timedelta(seconds=121)
        self.store.ingest(second)
        self.assertEqual(self.store.handshake(second["source_instance_id"], 2, second["snapshot_sha256"]), "MATCH")
        original = self.store.projection
        injected_once = False

        def adversarial_projection(**kwargs):
            nonlocal injected_once
            def concurrent_head_advance():
                nonlocal injected_once
                if not injected_once:
                    injected_once = True
                    self.assertEqual(original()[0]["snapshot_sequence"], 2)
            return original(after_read=concurrent_head_advance)

        self.store.projection = adversarial_projection
        status, _headers, body = self.request("GET", "/api/v1/synthetic/missions", token="viewer-demo")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["snapshot_sequence"], 2)
        self.assertEqual(data["sync_state"], "FRESH")
        self.assertEqual(self.store.health()["snapshot_sequence"], 2)

    def test_get_storage_failure_returns_safe_503_and_recovers(self):
        self.store.ingest(self.snapshot)
        self.assertEqual(self.request("GET", "/api/v1/synthetic/health", token="viewer-demo")[0], 200)
        corrupt_path = Path(self.temp.name) / "corrupt.db"
        sqlite3.connect(corrupt_path).close()
        self.store.path = corrupt_path
        for path in ("/api/v1/synthetic/health", "/api/v1/synthetic/missions"):
            status, _headers, body = self.request("GET", path, token="viewer-demo")
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["sync_state"], "UNAVAILABLE")
            self.assertNotIn(b"Traceback", body)
            self.assertNotIn(b"sqlite", body.lower())
        healthy_path = Path(self.temp.name) / "healthy-again.db"
        healthy_store = SyntheticProjectionStore(healthy_path, clock=self.clock)
        healthy_store.ingest(self.snapshot)
        self.store.path = healthy_path
        self.assertEqual(self.request("GET", "/api/v1/synthetic/health", token="viewer-demo")[0], 200)


if __name__ == "__main__":
    unittest.main()
