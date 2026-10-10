"""BUBUS2.0 Risk preparation stays read-only and cannot arm orders."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bubus.risk_readiness import (
    RiskReadinessError, assess_draft_order_cap, assess_risk_readiness, validate_draft,
)
from bubus.server import make_server
from tests.assistant.test_market_dashboard import FakeCoinGeckoOverview, FakeCryptoOverview

_NOW = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)


def reconciliation_stub():
    return {
        "status": "BLOCKED_INCOMPLETE",
        "authority_status": "RECONCILIATION_EVIDENCE_ONLY",
        "usable_for_pnl": False, "usable_for_risk": False,
        "usable_for_execution": False, "orders_enabled": False,
        "pnl": None, "evidence_sha256": "a" * 64,
        "reasons": ["PER_ASSET_POSTINGS_MISSING"],
    }


def audited(*, draft=None, portfolio=None, valuations=None,
            reconciled=None, gate=False):
    return assess_risk_readiness(
        draft=draft, portfolio=portfolio,
        valuations=[] if valuations is None else valuations,
        reconciliation=reconciliation_stub() if reconciled is None else reconciled,
        order_gate_connected=gate,
    )


class RiskReadinessUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = __import__("bubus.store", fromlist=["DeskStore"]).DeskStore(
            Path(self.temp.name) / "desk.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def draft(self):
        return self.store.propose_risk_limits({
            "base_currency": "EUR", "capital_limit": "1000.50",
            "max_order_notional": "100.25", "max_daily_loss": "25.00",
        })

    def test_no_limits_never_defaults_and_remains_blocked(self):
        result = audited()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["draft_status"], "MISSING")
        self.assertIn("OWNER_LIMITS_MISSING", result["reason_codes"])
        self.assertIn("BROKER_PORTFOLIO_MISSING", result["reason_codes"])
        self.assertFalse(result["risk_policy_active"])
        self.assertFalse(result["risk_approved"])
        self.assertFalse(result["orders_enabled"])
        self.assertFalse(result["usable_for_execution"])
        self.assertFalse(result["armed"])
        self.assertEqual(result, audited())

    def test_owner_draft_is_validated_without_becoming_policy(self):
        draft = self.draft()
        checked = validate_draft(draft)
        self.assertEqual(checked["max_order_notional"], "100.25")
        result = audited(draft=draft)
        self.assertEqual(result["draft_status"], "PENDING_OWNER_REVIEW")
        self.assertIn("COMPLETE_RISK_POLICY_MISSING", result["reason_codes"])
        self.assertEqual(result["evidence"]["draft_candidate_id"], draft["candidate_id"])
        self.assertEqual(result["evidence"]["draft_sha256"], draft["payload_sha256"])
        self.assertFalse(result["risk_policy_active"])
        self.assertFalse(result["orders_enabled"])

    def test_draft_limit_comparisons_never_enable_trading(self):
        draft = self.draft()
        below = assess_draft_order_cap(draft=draft, currency="EUR", order_notional="99.50")
        above = assess_draft_order_cap(draft=draft, currency="EUR", order_notional="100.26")
        fx = assess_draft_order_cap(draft=draft, currency="USD", order_notional="1")
        self.assertEqual(below["limit_comparison"], "PASS_LIMITS_ONLY")
        self.assertEqual(below["status"], "BLOCKED")
        self.assertIn("DAILY_PNL_UNVERIFIED", below["reasons"])
        self.assertFalse(below["orders_enabled"])
        self.assertEqual(above["limit_comparison"], "REJECT_LIMITS")
        self.assertIn("MAX_ORDER_NOTIONAL_EXCEEDED", above["reasons"])
        self.assertIn("FX_UNVERIFIED", fx["reasons"])
        for invalid in (None, "", 12.0, "NaN", "Infinity", "-1", "0", "1e9"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(RiskReadinessError):
                    assess_draft_order_cap(draft=draft, currency="EUR", order_notional=invalid)

    def test_tampered_draft_and_unapproved_flags_are_rejected(self):
        draft = self.draft()
        variants = [
            dict(draft, capital_limit="9999"),
            dict(draft, max_daily_loss="100000"),
            dict(draft, approval_status="APPROVED"),
            dict(draft, orders_enabled=True),
            dict(draft, active=True),
            dict(draft, base_currency="USD"),
            dict(draft, created_at="2026-10-10T10:00:00"),
            dict(draft, max_order_notional="NaN"),
            dict(draft, candidate_id=True),
        ]
        for invalid in variants:
            with self.subTest(invalid=invalid):
                with self.assertRaises(RiskReadinessError):
                    audited(draft=invalid)

    def test_unexpected_order_gate_is_alarm_not_approval(self):
        report = audited(draft=self.draft(), gate=True)
        self.assertTrue(report["execution_gate_connected"])
        self.assertIn("UNEXPECTED_ORDER_GATE_CONNECTED", report["reason_codes"])
        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["orders_enabled"])

    def test_authoritative_reconciliation_or_portfolio_is_not_accepted(self):
        recon = dict(reconciliation_stub(), status="MATCHED_ARITHMETIC_ONLY")
        with self.assertRaises(RiskReadinessError):
            audited(reconciled=recon)
        recon = dict(reconciliation_stub(), pnl="42")
        with self.assertRaises(RiskReadinessError):
            audited(reconciled=recon)
        portfolio = {"provider": "Crypto.com Exchange",
                     "authority_status": "OBSERVATION_ONLY",
                     "usable_for_risk": True, "usable_for_execution": False,
                     "orders_enabled": False, "snapshot_id": 1,
                     "fetched_at": _NOW.isoformat(),
                     "stored_payload_sha256": "a" * 64}
        with self.assertRaises(RiskReadinessError):
            audited(portfolio=portfolio)

    def test_period_observation_never_counts_as_daily_pnl_or_arms_orders(self):
        recon = dict(reconciliation_stub(), period_pnl={
            "amount": "0.0017365755", "currency": "EUR",
            "authority_status": "RECONCILED_PERIOD_OBSERVATION_ONLY",
            "daily_pnl": False,
        })
        report = audited(draft=self.draft(), reconciled=recon)
        self.assertFalse(report["daily_pnl_verified"])
        self.assertIn("DAILY_PNL_UNVERIFIED", report["reason_codes"])
        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["orders_enabled"])
        self.assertFalse(report["usable_for_execution"])


class RiskReadinessRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(
            0, data_dir=self.temp.name,
            cryptocom_market_overview=FakeCryptoOverview(),
            coingecko_market_overview=FakeCoinGeckoOverview(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def test_route_is_read_only_same_origin_and_disarmed(self):
        with urlopen(self.url + "/api/desk/risk-readiness") as response:
            result = json.load(response)["readiness"]
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["draft_status"], "MISSING")
        self.assertFalse(result["execution_gate_connected"])
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(result["authority_status"], "READ_ONLY_RISK_PREPARATION")
        req = Request(self.url + "/api/desk/risk-readiness",
                      headers={"Origin": "https://foreign.example"})
        with self.assertRaises(HTTPError) as refused:
            urlopen(req)
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()

    def test_desktop_ui_exposes_blockers_but_no_arm_action(self):
        with urlopen(self.url) as response:
            html = response.read().decode("utf-8")
        with urlopen(self.url + "/assets/desk.js") as response:
            script = response.read().decode("utf-8")
        with urlopen(self.url + "/assets/desk.css") as response:
            styles = response.read().decode("utf-8")
        self.assertIn("view-autopilot", html)
        self.assertIn("aucune politique ni aucun ordre n’est activé", html)
        self.assertIn("Contrôle de préparation Risk", script)
        self.assertIn("/api/desk/risk-readiness", script)
        self.assertIn("BLOQUÉ · trading non armé", script)
        self.assertIn("loadRiskReadiness();", script)
        self.assertIn(".risk-audit-panel", styles)
        self.assertNotIn("create_limit_order(", script)

    def test_draft_is_not_approval_and_corruption_returns_503(self):
        candidate = self.server.desk_store.propose_risk_limits({
            "base_currency": "EUR", "capital_limit": "600.00",
            "max_order_notional": "60.00", "max_daily_loss": "12.00",
        })
        with urlopen(self.url + "/api/desk/risk-readiness") as response:
            readiness = json.load(response)["readiness"]
        self.assertEqual(readiness["draft_status"], "PENDING_OWNER_REVIEW")
        self.assertEqual(readiness["evidence"]["draft_candidate_id"], candidate["candidate_id"])
        self.assertFalse(readiness["orders_enabled"])
        db = sqlite3.connect(Path(self.temp.name) / "desk.sqlite3")
        try:
            db.execute("UPDATE risk_limit_candidates SET capital_limit='1000000'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(HTTPError) as refused:
            urlopen(self.url + "/api/desk/risk-readiness")
        self.assertEqual(refused.exception.code, 503)
        refused.exception.close()

    def test_rehashed_portfolio_authority_tamper_blocks_risk_and_portfolio_api(self):
        self.server.desk_store.record_cryptocom_portfolio({
            "provider": "Crypto.com Exchange", "fetched_at": _NOW.isoformat(),
            "payload_sha256": "b" * 64,
        })
        with urlopen(self.url + "/api/desk/risk-readiness") as response:
            safe = json.load(response)["readiness"]
        self.assertEqual(safe["status"], "BLOCKED")
        db = sqlite3.connect(Path(self.temp.name) / "desk.sqlite3")
        try:
            raw, = db.execute("SELECT payload_json FROM cryptocom_portfolio_snapshots").fetchone()
            tampered = json.loads(raw)
            tampered["usable_for_risk"] = True
            encoded = json.dumps(tampered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            db.execute("UPDATE cryptocom_portfolio_snapshots SET payload_json=?,payload_sha256=?",
                       (encoded, digest))
            db.commit()
        finally:
            db.close()
        for path in ("/api/desk/risk-readiness", "/api/desk/portfolio"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as refused:
                    urlopen(self.url + path)
                self.assertEqual(refused.exception.code, 503)
                refused.exception.close()

    def test_no_post_endpoint_grants_risk_authorization(self):
        request = Request(
            self.url + "/api/desk/risk-readiness",
            data=b'{"enable_orders":true}', method="POST",
            headers={"Origin": self.url, "Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as refused:
            urlopen(request)
        self.assertIn(refused.exception.code, {400, 403, 404, 405, 409, 501})
        refused.exception.close()
        with urlopen(self.url + "/api/desk/state") as response:
            state = json.load(response)["trading"]
        self.assertFalse(state["orders_enabled"])
        self.assertFalse(state["armed"])


if __name__ == "__main__":
    unittest.main()
