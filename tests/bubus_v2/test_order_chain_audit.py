"""Offline order-chain fault injection: no credentials, no network, no execution."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bubus.order_chain_audit import (
    OrderChainAuditError, audit_order_chain_configuration, replay_offline_order_evidence,
)
from bubus.server import make_server
from tests.assistant.test_market_dashboard import FakeCoinGeckoOverview, FakeCryptoOverview

OID = "fa14b16b-6b53-4716-8e78-253ad1cfe600"
DIGEST = "a" * 64
T0 = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
_INTENT = {"client_oid": OID, "intent_sha256": DIGEST, "quantity": "0.10"}


def event(step, second, *, oid=OID, digest=DIGEST, order_id=None,
          status=None, cumulative=None):
    return {"step": step, "client_oid": oid, "intent_sha256": digest,
            "order_id": order_id, "status": status, "cumulative_quantity": cumulative,
            "observed_at": (T0 + timedelta(seconds=second)).isoformat()}


def detail(second, status="ACTIVE", cumulative="0", order_id="123"):
    return event("READBACK", second, order_id=order_id, status=status, cumulative=cumulative)


class OfflineOrderChainTests(unittest.TestCase):
    def replay(self, events):
        return replay_offline_order_evidence(intent=_INTENT, events=events)

    def test_no_events_or_unresolved_post_is_always_blocked(self):
        empty = self.replay([])
        self.assertEqual(empty["status"], "BLOCKED_INCOMPLETE")
        self.assertIn("ORDER_JOURNAL_EMPTY", empty["reasons"])
        self.assertFalse(empty["can_retry_automatically"])
        self.assertFalse(empty["can_submit"])
        for events in (
            [event("ATTEMPT", 0)],
            [event("ATTEMPT", 0), event("RESPONSE_UNCERTAIN", 1)],
            [event("ATTEMPT", 0), event("ACK", 1, order_id="123")],
            [event("ATTEMPT", 0), event("READBACK_FAILED", 1)],
        ):
            report = self.replay(events)
            self.assertEqual(report["status"], "BLOCKED_UNCERTAIN")
            self.assertIn("SUBMISSION_STATE_UNRESOLVED", report["reasons"])
            self.assertTrue(report["needs_manual_readback"])
            self.assertFalse(report["orders_enabled"])
            self.assertFalse(report["can_retry_automatically"])

    def test_readback_after_timeout_never_authorizes_repeat_order(self):
        rows = [event("ATTEMPT", 0), event("RESPONSE_UNCERTAIN", 1),
                detail(2, "CANCELED", "0.03")]
        report = self.replay(rows)
        self.assertEqual(report["status"], "BLOCKED_INCOMPLETE")
        self.assertFalse(report["needs_manual_readback"])
        self.assertEqual(report["cumulative_quantity_observed"], "0.03")
        self.assertEqual(report["last_exchange_status_observed"], "CANCELED")
        self.assertIn("EXECUTED_QUANTITY_MUST_RECONCILE_WITH_BALANCES", report["reasons"])
        self.assertFalse(report["can_retry_automatically"])
        self.assertFalse(report["usable_for_execution"])
        self.assertEqual(report, self.replay(rows))

    def test_duplicate_post_even_with_same_hash_is_conflict(self):
        result = self.replay([event("ATTEMPT", 0), event("RESPONSE_UNCERTAIN", 1),
                              event("ATTEMPT", 2)])
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["status"], "BLOCKED_CONTRADICTORY")
        self.assertIn("DUPLICATE_SUBMISSION_ATTEMPT", result["reasons"])

    def test_intent_swap_and_order_identity_swap_detected(self):
        events = [event("ATTEMPT", 0), event("ACK", 1, order_id="123"),
                  detail(2, order_id="456"),
                  detail(3, order_id="789")]
        report = self.replay(events)
        self.assertEqual(report["status"], "BLOCKED_CONTRADICTORY")
        self.assertIn("ACK_READBACK_ORDER_ID_MISMATCH", report["reasons"])
        self.assertIn("READBACK_ORDER_ID_CHANGED", report["reasons"])
        swap = self.replay([event("ATTEMPT", 0),
                            event("READBACK_FAILED", 1, digest="b" * 64)])
        self.assertIn("INTENT_OR_CLIENT_OID_CHANGED", swap["reasons"])

    def test_stale_updates_regression_and_terminal_reversal(self):
        report = self.replay([event("ATTEMPT", 2), detail(3, "FILLED", "0.07"),
                              detail(1, "ACTIVE", "0.02")])
        self.assertEqual(report["status"], "BLOCKED_CONTRADICTORY")
        self.assertIn("NON_MONOTONIC_EVENT_CLOCK", report["reasons"])
        self.assertIn("CUMULATIVE_FILL_REGRESSED", report["reasons"])
        self.assertIn("TERMINAL_STATUS_REVERSED", report["reasons"])

    def test_filled_larger_than_order_or_zero_is_blocked(self):
        for record, reason in (
            (detail(1, "FILLED", "0"), "ZERO_FILL_MARKED_FILLED"),
            (detail(1, "FILLED", "0.11"), "EXECUTION_EXCEEDS_INTENT"),
        ):
            report = self.replay([event("ATTEMPT", 0), record])
            self.assertIn(reason, report["reasons"])
            self.assertEqual(report["status"], "BLOCKED_CONTRADICTORY")
            self.assertFalse(report["can_submit"])

    def test_filled_state_requires_exact_original_quantity(self):
        report = self.replay([event("ATTEMPT", 0), detail(2, "FILLED", "0.08")])
        self.assertEqual(report["status"], "BLOCKED_CONTRADICTORY")
        self.assertIn("FILLED_QUANTITY_MISMATCH", report["reasons"])
        self.assertFalse(report["can_retry_automatically"])

    def test_unknown_status_or_invalid_amount_fails_closed(self):
        for bad in (
            detail(1, "BOGUS", "0.01"), detail(1, "FILLED", "-1"),
            detail(1, "FILLED", "NaN"), detail(1, "FILLED", "Infinity"),
            event("ACK", 1, order_id="bad-id"),
            event("RESPONSE_UNCERTAIN", 1, status="FILLED"),
            event("READBACK", 1, order_id="123", status="FILLED", cumulative="1e20"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(OrderChainAuditError):
                    self.replay([event("ATTEMPT", 0), bad])

    def test_offline_evidence_cannot_prove_risk_or_durable_gate(self):
        report = self.replay([event("ATTEMPT", 0), event("ACK", 1, order_id="123"),
                              detail(2, "FILLED", "0.10")])
        self.assertEqual(report["status"], "BLOCKED_INCOMPLETE")
        self.assertFalse(report["can_submit"])
        self.assertFalse(report["orders_enabled"])
        self.assertIn("DURABLE_EXACT_INTENT_LEDGER_MISSING", report["reasons"])
        self.assertIn("SINGLE_USE_APPROVAL_GATE_MISSING", report["reasons"])
        self.assertEqual(len(report["audit_sha256"]), 64)

    def test_configuration_insists_on_risk_and_never_promotes_gate(self):
        risk = {"status": "BLOCKED", "armed": False, "orders_enabled": False,
                "risk_policy_active": False, "usable_for_execution": False}
        for connected in (False, True):
            report = audit_order_chain_configuration(
                gate_connected=connected, spot_verifier_connected=True, risk_readiness=risk)
            self.assertEqual(report["status"], "BLOCKED")
            self.assertFalse(report["can_submit"])
            self.assertFalse(report["retry_automatically"])
            self.assertFalse(report["orders_enabled"])
            self.assertIn("DURABLE_ORDER_JOURNAL_MISSING", report["reasons"])
            if connected:
                self.assertIn("UNEXPECTED_ORDER_GATE_CONNECTED", report["reasons"])
        journal_ready = audit_order_chain_configuration(
            gate_connected=False, spot_verifier_connected=True, risk_readiness=risk,
            durable_journal_ready=True)
        self.assertTrue(journal_ready["durable_journal_ready"])
        self.assertNotIn("DURABLE_ORDER_JOURNAL_MISSING", journal_ready["reasons"])
        self.assertNotIn("CROSS_RESTART_IDEMPOTENCY_UNVERIFIED", journal_ready["reasons"])
        self.assertIn("REAL_ORDER_ROUTES_DISABLED", journal_ready["reasons"])
        self.assertFalse(journal_ready["can_submit"])
        readback_ready = audit_order_chain_configuration(
            gate_connected=False, spot_verifier_connected=True, risk_readiness=risk,
            durable_journal_ready=True, manual_readback_route_connected=True)
        self.assertTrue(readback_ready["manual_readback_available"])
        self.assertFalse(readback_ready["recovery_ready"])
        self.assertNotIn("UNCERTAIN_POST_RESPONSE_RECOVERY_NOT_IMPLEMENTED", readback_ready["reasons"])
        self.assertFalse(readback_ready["can_submit"])
        with self.assertRaises(OrderChainAuditError):
            audit_order_chain_configuration(
                gate_connected=False, spot_verifier_connected=True,
                risk_readiness=dict(risk, status="READY"))


class ReadOnlyOrderChainRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(0, data_dir=self.temp.name,
            cryptocom_market_overview=FakeCryptoOverview(),
            coingecko_market_overview=FakeCoinGeckoOverview())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def test_default_bubus_adapter_has_no_gate_or_order_submission_endpoint(self):
        self.assertIsNone(self.server.cryptocom_spot_order_adapter._gate)
        with urlopen(self.url + "/api/desk/order-chain-audit") as response:
            result = json.load(response)["audit"]
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["order_gate_connected"])
        self.assertFalse(result["orders_enabled"])
        self.assertFalse(result["can_submit"])
        self.assertFalse(result["retry_automatically"])
        self.assertTrue(result["durable_journal_ready"])
        self.assertTrue(result["manual_readback_available"])
        self.assertNotIn("UNCERTAIN_POST_RESPONSE_RECOVERY_NOT_IMPLEMENTED", result["reasons"])
        self.assertNotIn("DURABLE_ORDER_JOURNAL_MISSING", result["reasons"])
        self.assertEqual(result["authority_status"], "ORDER_CHAIN_READ_ONLY_AUDIT")
        with urlopen(self.url + "/api/desk/live-order-journal") as response:
            journal = json.load(response)["journal"]
        self.assertEqual(journal["status"], "INTEGRITY_OK")
        self.assertEqual(journal["authority_status"], "LIVE_ORDER_JOURNAL_EVIDENCE_ONLY")
        self.assertFalse(journal["orders_enabled"])
        self.assertEqual(journal["intents"], [])
        with urlopen(self.url + "/api/desk/live-order-journal") as response:
            self.assertEqual(json.load(response)["journal"]["intents"], [])
        with urlopen(self.url + "/api/desk/state") as response:
            trading = json.load(response)["trading"]
        self.assertEqual(trading["state"], "NOT_READY")
        self.assertFalse(trading["armed"])

    def test_private_order_readback_requires_a_recorded_attempt_and_persists_exchange_status(self):
        from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder
        from invest_ai.portfolio.cryptocom_credential_store import CryptoComCredentials

        class ReadOnlyCredentialStore:
            available = True
            def load(self):
                return CryptoComCredentials("read-only-key", "read-only-secret")

        class OrderDetailTransport:
            calls = []
            def request(self, method, api_key, api_secret, params):
                self.calls.append((method, api_key, api_secret, dict(params)))
                return {"method": "private/get-order-detail", "code": 0, "result": {
                    "order_id": "123", "client_oid": OID, "status": "ACTIVE",
                    "cumulative_quantity": "0.02", "cumulative_value": "1200",
                    "cumulative_fee": "0.60", "fee_instrument_name": "USDT",
                    "update_time": int(datetime.now(timezone.utc).timestamp() * 1000),
                }}

        journal = self.server.live_order_journal
        order = CryptoComSpotLimitOrder(OID, "BTC_USDT", "BUY", Decimal("60000"), Decimal("0.10"))
        journal.reserve_intent(order)
        journal.record_authorization(OID, risk_receipt_id="risk-1", policy_revision="policy-1",
            control_epoch=1, human_approval_id="owner-1")
        journal.record_submission_attempt(OID, order.canonical_sha256())
        transport = OrderDetailTransport()
        self.server.cryptocom_spot_order_adapter._transport = transport
        self.server.cryptocom_credential_store = ReadOnlyCredentialStore()
        headers = {"Content-Type": "application/json", "Origin": self.url}
        body = json.dumps({"use_saved_credentials": True, "client_oid": OID}).encode()
        with urlopen(Request(self.url + "/api/cryptocom/orders/readback", data=body,
                             headers=headers)) as response:
            result = json.load(response)["order"]
        self.assertEqual(transport.calls, [("private/get-order-detail", "read-only-key", "read-only-secret",
                                            {"client_oid": OID})])
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(result["executed_quantity"], "0.02")
        self.assertEqual(result["authority_status"], "BROKER_ORDER_READBACK_EVIDENCE_ONLY")
        self.assertFalse(result["orders_enabled"])
        audit = journal.audit(OID)
        self.assertEqual(audit["intents"][0]["readback_count"], 1)
        self.assertEqual(audit["intents"][0]["exchange_order_id"], "123")

    def test_order_readback_does_not_query_unknown_or_foreign_order_identifiers(self):
        class NoCallTransport:
            calls = []
            def request(self, *args):
                self.calls.append(args)
                raise AssertionError("unknown order ID must not reach Crypto.com")

        transport = NoCallTransport()
        self.server.cryptocom_spot_order_adapter._transport = transport
        body = json.dumps({"use_saved_credentials": True, "client_oid": OID}).encode()
        with self.assertRaises(HTTPError) as unknown:
            urlopen(Request(self.url + "/api/cryptocom/orders/readback", data=body,
                headers={"Content-Type": "application/json", "Origin": self.url}))
        self.assertEqual(unknown.exception.code, 404)
        unknown.exception.close()
        with self.assertRaises(HTTPError) as foreign:
            urlopen(Request(self.url + "/api/cryptocom/orders/readback", data=body,
                headers={"Content-Type": "application/json", "Origin": "https://foreign.example"}))
        self.assertEqual(foreign.exception.code, 403)
        foreign.exception.close()
        self.assertEqual(transport.calls, [])

    def test_ui_exposes_read_only_order_chain_blockers(self):
        with urlopen(self.url + "/assets/desk.js") as response:
            script = response.read().decode("utf-8")
        self.assertIn("Audit de la chaîne d’ordres Crypto.com", script)
        self.assertIn("function loadOrderChainAudit()", script)
        self.assertIn("/api/desk/order-chain-audit", script)
        self.assertIn("BLOQUÉ · aucun ordre · aucune reprise automatique", script)
        self.assertIn("Journal durable : intégrité contrôlée", script)
        self.assertIn("/api/desk/live-order-journal", script)
        self.assertIn("/api/cryptocom/orders/readback", script)
        self.assertIn("Aucune tentative d’ordre réel enregistrée.", script)
        self.assertIn("typeof report.durable_journal_ready !== 'boolean'", script)
        self.assertNotIn("create_limit_order(", script)

    def test_private_order_readback_requires_a_recorded_attempt_and_persists_exchange_status(self):
        from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder
        from invest_ai.portfolio.cryptocom_credential_store import CryptoComCredentials

        class ReadOnlyCredentialStore:
            available = True
            def load(self):
                return CryptoComCredentials("read-only-key", "read-only-secret")

        class OrderDetailTransport:
            calls = []
            def request(self, method, api_key, api_secret, params):
                self.calls.append((method, api_key, api_secret, dict(params)))
                return {"method": "private/get-order-detail", "code": 0, "result": {
                    "order_id": "123", "client_oid": OID, "status": "ACTIVE",
                    "cumulative_quantity": "0.02", "cumulative_value": "1200",
                    "cumulative_fee": "0.60", "fee_instrument_name": "USDT",
                    "update_time": int(datetime.now(timezone.utc).timestamp() * 1000),
                }}

        journal = self.server.live_order_journal
        order = CryptoComSpotLimitOrder(OID, "BTC_USDT", "BUY", Decimal("60000"), Decimal("0.10"))
        journal.reserve_intent(order)
        journal.record_authorization(OID, risk_receipt_id="risk-1", policy_revision="policy-1",
            control_epoch=1, human_approval_id="owner-1")
        journal.record_submission_attempt(OID, order.canonical_sha256())
        transport = OrderDetailTransport()
        self.server.cryptocom_spot_order_adapter._transport = transport
        self.server.cryptocom_credential_store = ReadOnlyCredentialStore()
        headers = {"Content-Type": "application/json", "Origin": self.url}
        body = json.dumps({"use_saved_credentials": True, "client_oid": OID}).encode()
        with urlopen(Request(self.url + "/api/cryptocom/orders/readback", data=body,
                             headers=headers)) as response:
            result = json.load(response)["order"]
        self.assertEqual(transport.calls, [("private/get-order-detail", "read-only-key", "read-only-secret",
                                            {"client_oid": OID})])
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(result["executed_quantity"], "0.02")
        self.assertEqual(result["authority_status"], "BROKER_ORDER_READBACK_EVIDENCE_ONLY")
        self.assertFalse(result["orders_enabled"])
        audit = journal.audit(OID)
        self.assertEqual(audit["intents"][0]["readback_count"], 1)
        self.assertEqual(audit["intents"][0]["exchange_order_id"], "123")

    def test_order_readback_does_not_query_unknown_or_foreign_order_identifiers(self):
        class NoCallTransport:
            calls = []
            def request(self, *args):
                self.calls.append(args)
                raise AssertionError("unknown order ID must not reach Crypto.com")

        transport = NoCallTransport()
        self.server.cryptocom_spot_order_adapter._transport = transport
        body = json.dumps({"use_saved_credentials": True, "client_oid": OID}).encode()
        with self.assertRaises(HTTPError) as unknown:
            urlopen(Request(self.url + "/api/cryptocom/orders/readback", data=body,
                headers={"Content-Type": "application/json", "Origin": self.url}))
        self.assertEqual(unknown.exception.code, 404)
        unknown.exception.close()
        with self.assertRaises(HTTPError) as foreign:
            urlopen(Request(self.url + "/api/cryptocom/orders/readback", data=body,
                headers={"Content-Type": "application/json", "Origin": "https://foreign.example"}))
        self.assertEqual(foreign.exception.code, 403)
        foreign.exception.close()
        self.assertEqual(transport.calls, [])

    def test_tampered_risk_draft_blocks_order_chain_audit(self):
        import sqlite3
        self.server.desk_store.propose_risk_limits({
            "base_currency": "EUR", "capital_limit": "1000",
            "max_order_notional": "100", "max_daily_loss": "50",
        })
        db = sqlite3.connect(Path(self.temp.name) / "desk.sqlite3")
        try:
            db.execute("UPDATE risk_limit_candidates SET max_order_notional='999999'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(HTTPError) as refused:
            urlopen(self.url + "/api/desk/order-chain-audit")
        self.assertEqual(refused.exception.code, 503)
        refused.exception.close()

    def test_cross_origin_denied_and_post_must_not_arm(self):
        req = Request(self.url + "/api/desk/order-chain-audit",
                      headers={"Origin": "https://evil.example"})
        with self.assertRaises(HTTPError) as denied:
            urlopen(req)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        req = Request(self.url + "/api/desk/order-chain-audit",
                      data=b'{"activate":true}', method="POST",
                      headers={"Origin": self.url, "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as denied:
            urlopen(req)
        self.assertIn(denied.exception.code, {400, 403, 404, 405, 409, 501})
        denied.exception.close()


if __name__ == "__main__":
    unittest.main()
