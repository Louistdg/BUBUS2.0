"""Risk reservation rehearsal tests: synthetic capital only, never broker orders."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json

from bubus.offline_risk_reservations import OfflineRiskReservations, OfflineRiskError
from bubus.server import make_server
from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder
from tests.assistant.test_market_dashboard import FakeCoinGeckoOverview, FakeCryptoOverview

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
OID_A = str(uuid.UUID(int=1000))
OID_B = str(uuid.UUID(int=2000))
OID_C = str(uuid.UUID(int=3000))
AID_A = str(uuid.UUID(int=1001))
AID_B = str(uuid.UUID(int=2001))
AID_C = str(uuid.UUID(int=3001))


def intent(oid=OID_A, *, price="40.00", quantity="1.00", side="BUY", pair="BTC_USDT"):
    return CryptoComSpotLimitOrder(
        client_oid=oid, instrument_name=pair, side=side,
        price=Decimal(price), quantity=Decimal(quantity))


def configure(journal, **overrides):
    kwargs = dict(
        quote_currency="USDT", capital_limit="100", max_order_notional="70",
        max_daily_loss="20", observed_daily_loss="2", fee_cushion="0.01",
    )
    kwargs.update(overrides)
    return journal.set_synthetic_budget(**kwargs)


def approval(journal, order, aid, *, expiry=None, approver="SYNTHETIC_REVIEWER"):
    return journal.record_synthetic_human_approval(
        order, approval_id=aid, approver_label=approver,
        expires_at=NOW + timedelta(minutes=5) if expiry is None else expiry)


class OfflineRiskReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "offline-risk-reservations.sqlite3"
        self.clock = [NOW]
        self.journal = OfflineRiskReservations(self.path, clock=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def reopen(self):
        return OfflineRiskReservations(self.path, clock=lambda: self.clock[0])

    def test_no_budget_stays_unusable_and_never_infers_capital(self):
        audit = self.journal.audit()
        self.assertEqual(audit["status"], "BLOCKED_SIMULATION_ONLY")
        self.assertIsNone(audit["capital_limit"])
        self.assertIsNone(audit["remaining_synthetic_capital"])
        self.assertEqual(audit["total_approvals"], 0)
        self.assertFalse(audit["can_submit"])
        self.assertFalse(audit["connected_to_order_adapter"])
        with self.assertRaisesRegex(OfflineRiskError, "Explicit simulation capital"):
            approval(self.journal, intent(), AID_A)
        self.assertEqual(audit, self.reopen().audit())

    def test_explicit_budget_hash_and_zero_real_authority(self):
        report = configure(self.journal)
        self.assertEqual(report["source"], "EXPLICIT_SYNTHETIC_TEST_FIXTURE")
        self.assertEqual(report["capital_limit"], "100")
        self.assertEqual(report["reserved_capital"], "0")
        self.assertEqual(report["remaining_synthetic_capital"], "100")
        self.assertTrue(report["reserve_and_consume_atomic"])
        self.assertFalse(report["approval_is_real_human_authentication"])
        self.assertFalse(report["cross_journal_atomicity"])
        self.assertFalse(report["usable_for_risk"])
        self.assertFalse(report["orders_enabled"])
        with self.assertRaisesRegex(OfflineRiskError, "already exists"):
            configure(self.reopen())

    def test_approval_consumed_and_funds_reserved_atomically(self):
        configure(self.journal)
        order = intent()
        approval(self.journal, order, AID_A)
        before = self.journal.audit()
        self.assertEqual(before["pending_approvals"], 1)
        self.assertEqual(before["reserved_capital"], "0")
        report = self.journal.consume_and_reserve_simulated(approval_id=AID_A, intent=order)
        self.assertEqual(report["pending_approvals"], 0)
        self.assertEqual(report["consumed_approvals"], 1)
        self.assertEqual(report["reserved_capital"], "40.40000000")
        self.assertEqual(report["remaining_synthetic_capital"], "59.60000000")
        self.assertEqual(report["entries"][0]["state"], "RESERVED")
        self.assertFalse(report["entries"][0]["can_submit"])
        self.assertIsNotNone(report["entries"][0]["consumed_at"])
        self.assertEqual(report, self.reopen().audit())
        with self.assertRaisesRegex(OfflineRiskError, "already consumed"):
            self.reopen().consume_and_reserve_simulated(approval_id=AID_A, intent=order)
        self.assertEqual(self.journal.audit(), report)

    def test_approval_rejects_mismatched_intent_before_spending(self):
        configure(self.journal)
        original = intent()
        approval(self.journal, original, AID_A)
        for changed in (intent(price="40.01"), intent(quantity="0.5"), intent(oid=OID_B)):
            with self.subTest(changed=changed):
                with self.assertRaises(OfflineRiskError):
                    self.journal.consume_and_reserve_simulated(approval_id=AID_A, intent=changed)
                self.assertEqual(self.journal.audit()["reserved_capital"], "0")
        self.assertEqual(self.journal.audit()["pending_approvals"], 1)

    def test_capital_overcommit_does_not_consume_failed_approval(self):
        configure(self.journal)
        a = intent(price="60")
        b = intent(oid=OID_B, price="50")
        approval(self.journal, a, AID_A)
        approval(self.journal, b, AID_B)
        self.journal.consume_and_reserve_simulated(approval_id=AID_A, intent=a)
        with self.assertRaisesRegex(OfflineRiskError, "Insufficient"):
            self.journal.consume_and_reserve_simulated(approval_id=AID_B, intent=b)
        report = self.journal.audit()
        self.assertEqual(report["reserved_capital"], "60.60000000")
        self.assertEqual(report["consumed_approvals"], 1)
        self.assertEqual(report["pending_approvals"], 1)
        self.assertEqual(report, self.reopen().audit())

    def test_simultaneous_workers_cannot_double_spend(self):
        configure(self.journal)
        a, b = intent(price="60"), intent(oid=OID_B, price="50")
        approval(self.journal, a, AID_A)
        approval(self.journal, b, AID_B)
        barrier = threading.Barrier(3)
        def compete(order, aid):
            worker = self.reopen()
            barrier.wait()
            try:
                worker.consume_and_reserve_simulated(approval_id=aid, intent=order)
                return "RESERVED"
            except OfflineRiskError:
                return "BLOCKED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(compete, a, AID_A)
            second = pool.submit(compete, b, AID_B)
            barrier.wait()
            outcomes = sorted([first.result(timeout=10), second.result(timeout=10)])
        self.assertEqual(outcomes, ["BLOCKED", "RESERVED"])
        audit = self.reopen().audit()
        self.assertEqual(audit["consumed_approvals"], 1)
        self.assertLess(Decimal(audit["reserved_capital"]), Decimal("100"))

    def test_concurrent_replay_of_one_approval_is_single_use(self):
        configure(self.journal)
        order = intent()
        approval(self.journal, order, AID_A)
        barrier = threading.Barrier(3)
        def compete():
            barrier.wait()
            try:
                self.reopen().consume_and_reserve_simulated(approval_id=AID_A, intent=order)
                return "RESERVED"
            except OfflineRiskError:
                return "BLOCKED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(compete)
            second = pool.submit(compete)
            barrier.wait()
            results = sorted([first.result(timeout=10), second.result(timeout=10)])
        self.assertEqual(results, ["BLOCKED", "RESERVED"])
        self.assertEqual(self.reopen().audit()["reserved_capital"], "40.40000000")

    def test_approval_expiry_and_clock_regression_are_fail_closed(self):
        configure(self.journal)
        order = intent()
        approval(self.journal, order, AID_A)
        self.clock[0] = NOW + timedelta(minutes=5)
        with self.assertRaisesRegex(OfflineRiskError, "expired"):
            self.journal.consume_and_reserve_simulated(approval_id=AID_A, intent=order)
        self.clock[0] = NOW - timedelta(minutes=1)
        with self.assertRaisesRegex(OfflineRiskError, "clock regressed"):
            self.journal.consume_and_reserve_simulated(approval_id=AID_A, intent=order)
        self.assertEqual(self.journal.audit()["pending_approvals"], 1)

    def test_daily_loss_threshold_blocks_creation_and_consumption(self):
        at_limit = self.reopen()
        configure(at_limit, observed_daily_loss="20")
        with self.assertRaisesRegex(OfflineRiskError, "daily loss"):
            approval(at_limit, intent(), AID_A)
        self.assertEqual(at_limit.audit()["total_approvals"], 0)

    def test_unsupported_sell_fx_and_fee_cushion_violation_fail_closed(self):
        configure(self.journal)
        with self.assertRaisesRegex(OfflineRiskError, "FX"):
            approval(self.journal, intent(pair="BTC_EUR"), AID_A)
        with self.assertRaisesRegex(OfflineRiskError, "SELL"):
            approval(self.journal, intent(side="SELL"), AID_A)
        # 69.5 * (1 + 0.01) > 70, even though raw order amount is below 70.
        with self.assertRaisesRegex(OfflineRiskError, "per-order"):
            approval(self.journal, intent(price="69.50"), AID_A)
        self.assertEqual(self.journal.audit()["total_approvals"], 0)

    def test_no_implicit_defaults_nonfinite_numbers_or_floating_args(self):
        for bad in ("NaN", "Infinity", "-1", "1e12", "0", 1.5, None, "", "1" * 30):
            with self.subTest(value=bad):
                with self.assertRaises(OfflineRiskError):
                    OfflineRiskReservations(Path(self.tmp.name) / ("test-" + str(uuid.uuid4()) + ".sqlite3")
                        ).set_synthetic_budget(
                        quote_currency="USDT", capital_limit=bad,
                        max_order_notional="70", max_daily_loss="20",
                        observed_daily_loss="2", fee_cushion="0.01")
        self.assertIsNone(self.journal.audit()["capital_limit"])

    def test_reserved_capital_never_released_by_expiry_or_restart(self):
        configure(self.journal)
        order = intent()
        approval(self.journal, order, AID_A)
        self.journal.consume_and_reserve_simulated(approval_id=AID_A, intent=order)
        self.clock[0] = NOW + timedelta(days=2)
        self.assertEqual(self.reopen().audit()["reserved_capital"], "40.40000000")
        with self.assertRaises(OfflineRiskError):
            self.reopen().consume_and_reserve_simulated(approval_id=AID_A, intent=order)
        db = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE synthetic_risk_approvals SET state='PENDING' WHERE approval_id=?", (AID_A,))
        finally:
            db.close()

    def test_crash_after_reservation_survives_separate_process(self):
        configure(self.journal)
        order = intent()
        approval(self.journal, order, AID_A)
        code = (
            "import os,sys;"
            "from pathlib import Path;"
            "from decimal import Decimal;"
            "from datetime import datetime,timezone;"
            "from bubus.offline_risk_reservations import OfflineRiskReservations;"
            "from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder;"
            "j=OfflineRiskReservations(Path(sys.argv[1]),clock=lambda:datetime(2026,10,10,12,tzinfo=timezone.utc));"
            "o=CryptoComSpotLimitOrder(client_oid=sys.argv[2],instrument_name='BTC_USDT',"
            "side='BUY',price=Decimal('40.00'),quantity=Decimal('1.00'));"
            "j.consume_and_reserve_simulated(approval_id=sys.argv[3],intent=o);"
            "os._exit(7)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, str(self.path), OID_A, AID_A],
            env=os.environ.copy(), capture_output=True, timeout=20)
        self.assertEqual(proc.returncode, 7, proc.stderr.decode(errors="replace"))
        reopened = self.reopen().audit()
        self.assertEqual(reopened["consumed_approvals"], 1)
        self.assertEqual(reopened["reserved_capital"], "40.40000000")
        with self.assertRaises(OfflineRiskError):
            self.reopen().consume_and_reserve_simulated(approval_id=AID_A, intent=order)

    def test_tampered_budget_or_approval_hash_refuses_entire_audit(self):
        configure(self.journal)
        approval(self.journal, intent(), AID_A)
        db = sqlite3.connect(self.path)
        try:
            db.execute("DROP TRIGGER synthetic_budget_immutable")
            db.execute("UPDATE synthetic_risk_budget SET capital_limit='999999'")
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(OfflineRiskError, "tampered"):
            self.reopen().audit()

    def test_approval_tamper_is_detected_and_direct_edit_blocked(self):
        configure(self.journal)
        approval(self.journal, intent(), AID_A)
        db = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE synthetic_risk_approvals SET reservation_amount='0'")
            db.execute("DROP TRIGGER synthetic_approval_no_identity_update")
            db.execute("UPDATE synthetic_risk_approvals SET reservation_amount='1'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(OfflineRiskError):
            self.reopen().audit()

    def test_immutable_consumption_receipt_and_tamper_detection(self):
        configure(self.journal)
        order = intent()
        approval(self.journal, order, AID_A)
        self.journal.consume_and_reserve_simulated(approval_id=AID_A, intent=order)
        db = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("DELETE FROM synthetic_risk_consumptions")
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE synthetic_risk_consumptions SET consumption_sha256=?", ("f" * 64,))
            db.execute("DROP TRIGGER synthetic_consumption_no_update")
            db.execute("UPDATE synthetic_risk_consumptions SET consumption_sha256=?", ("f" * 64,))
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(OfflineRiskError, "receipt hash"):
            self.reopen().audit()

    def test_unreceipted_reservation_is_rejected_even_with_valid_fields(self):
        configure(self.journal)
        approval(self.journal, intent(), AID_A)
        db = sqlite3.connect(self.path)
        try:
            db.execute(
                "UPDATE synthetic_risk_approvals SET state='RESERVED',consumed_at=? WHERE approval_id=?",
                (NOW.isoformat(), AID_A))
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(OfflineRiskError, "no durable consumption receipt"):
            self.reopen().audit()

    def test_process_crash_before_commit_rolls_back_partial_reservation(self):
        configure(self.journal)
        approval(self.journal, intent(), AID_A)
        code = (
            "import os,sqlite3,sys;"
            "db=sqlite3.connect(sys.argv[1],isolation_level=None);"
            "db.execute('BEGIN IMMEDIATE');"
            "db.execute(\"UPDATE synthetic_risk_approvals SET state='RESERVED',consumed_at='2026-10-10T12:00:00+00:00'\");"
            "os._exit(7)"
        )
        proc = subprocess.run([sys.executable, "-c", code, str(self.path)],
                              env=os.environ.copy(), capture_output=True, timeout=20)
        self.assertEqual(proc.returncode, 7, proc.stderr.decode(errors="replace"))
        report = self.reopen().audit()
        self.assertEqual(report["pending_approvals"], 1)
        self.assertEqual(report["consumed_approvals"], 0)
        self.assertEqual(report["reserved_capital"], "0")
        self.journal.consume_and_reserve_simulated(approval_id=AID_A, intent=intent())
        self.assertEqual(self.journal.audit()["reserved_capital"], "40.40000000")

    def test_wal_durable_and_immutable_policy(self):
        configure(self.journal)
        db = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE synthetic_risk_budget SET capital_limit='1000000'")
            rows = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        finally:
            db.close()
        self.assertIn(("synthetic_risk_budget",), rows)
        self.assertIn(("synthetic_risk_approvals",), rows)
        self.assertFalse(self.journal.audit()["connected_to_order_adapter"])


class OfflineRiskRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(
            0, data_dir=self.tmp.name,
            cryptocom_market_overview=FakeCryptoOverview(),
            coingecko_market_overview=FakeCoinGeckoOverview())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def test_api_is_read_only_and_has_no_default_money(self):
        with urlopen(self.url + "/api/desk/offline-risk-reservations") as response:
            audit = json.load(response)["reservations"]
        self.assertIsNone(audit["capital_limit"])
        self.assertFalse(audit["can_submit"])
        self.assertFalse(audit["orders_enabled"])
        self.assertEqual(audit["authority_status"], "SYNTHETIC_RISK_RESERVATION_ONLY")
        self.server.offline_risk_reservations.set_synthetic_budget(
            quote_currency="USDT", capital_limit="100", max_order_notional="70",
            max_daily_loss="20", observed_daily_loss="2", fee_cushion="0.01")
        with urlopen(self.url + "/api/desk/offline-risk-reservations") as response:
            filled = json.load(response)["reservations"]
        self.assertEqual(filled["capital_limit"], "100")
        self.assertFalse(filled["connected_to_order_adapter"])

    def test_cross_origin_refused_and_no_http_writes_exist(self):
        url = self.url + "/api/desk/offline-risk-reservations"
        with self.assertRaises(HTTPError) as err:
            urlopen(Request(url, headers={"Origin": "https://foreign.invalid"}))
        self.assertEqual(err.exception.code, 403)
        err.exception.close()
        with self.assertRaises(HTTPError) as err:
            urlopen(Request(url, data=b'{"allow_trade":true}', method="POST",
                            headers={"Origin": self.url, "Content-Type": "application/json"}))
        self.assertIn(err.exception.code, {400, 403, 404, 405, 409, 501})
        err.exception.close()
        with urlopen(self.url + "/api/desk/state") as response:
            trading = json.load(response)["trading"]
        self.assertFalse(trading["orders_enabled"])

    def test_corruption_returns_503_without_partial_financial_values(self):
        self.server.offline_risk_reservations.set_synthetic_budget(
            quote_currency="USDT", capital_limit="100", max_order_notional="70",
            max_daily_loss="20", observed_daily_loss="2", fee_cushion="0.01")
        db = sqlite3.connect(Path(self.tmp.name) / "offline-risk-reservations.sqlite3")
        try:
            db.execute("DROP TRIGGER synthetic_budget_immutable")
            db.execute("UPDATE synthetic_risk_budget SET capital_limit='9999'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(HTTPError) as err:
            urlopen(self.url + "/api/desk/offline-risk-reservations")
        self.assertEqual(err.exception.code, 503)
        err.exception.close()

    def test_frontend_does_not_expose_offline_risk_reservations(self):
        with urlopen(self.url + "/assets/desk.js") as response:
            script = response.read().decode("utf-8")
        self.assertNotIn("Réservations Risk — simulation", script)
        self.assertNotIn("/api/desk/offline-risk-reservations", script)
        self.assertNotIn("BLOCKED_SIMULATION_ONLY", script)
        self.assertNotIn("create_limit_order(", script)


if __name__ == "__main__":
    unittest.main()
