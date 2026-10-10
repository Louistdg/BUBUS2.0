"""Mission 5: durable OFFLINE-only order journal on local temporary SQLite."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import threading

from bubus.order_journal import OfflineOrderJournal, OrderJournalError
from bubus.server import make_server
from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder
from tests.assistant.test_market_dashboard import FakeCoinGeckoOverview, FakeCryptoOverview

_OID = "fa14b16b-6b53-4716-8e78-253ad1cfe600"
_NOW = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)


def intent(oid: str = _OID, quantity: str = "0.1000") -> CryptoComSpotLimitOrder:
    return CryptoComSpotLimitOrder(client_oid=oid, instrument_name="BTC_USDT",
                                    side="BUY", price=Decimal("60000.00"),
                                    quantity=Decimal(quantity))


class DurableOfflineJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "offline-order-journal.sqlite3"
        self.journal = OfflineOrderJournal(self.path, clock=lambda: _NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def restart(self):
        return OfflineOrderJournal(self.path, clock=lambda: _NOW)

    def test_intent_recorded_and_identity_survives_process_restart(self):
        first = self.journal.record_intent(intent())
        self.assertTrue(self.path.exists())
        self.assertEqual(first["event_count"], 1)
        self.assertEqual(first["attempt_count"], 0)
        self.assertFalse(first["needs_manual_reconciliation"])
        self.assertFalse(first["can_submit"])
        self.assertFalse(first["orders_enabled"])
        persisted = self.restart().get(_OID)
        self.assertEqual(persisted, first)
        self.assertEqual(self.restart().audit()["total_intents"], 1)

    def test_duplicate_oid_refused_across_restart_even_when_identical(self):
        self.journal.record_intent(intent())
        for repeated in (intent(), intent(quantity="0.02")):
            with self.subTest(repeated=repeated):
                with self.assertRaisesRegex(OrderJournalError, "already exists"):
                    self.restart().record_intent(repeated)
        self.assertEqual(self.journal.audit()["total_intents"], 1)
        self.assertEqual(self.journal.get(_OID)["event_count"], 1)

    def test_concurrent_different_process_like_instances_only_create_one_oid(self):
        a, b = self.journal, self.restart()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda journal: try_record(journal), [a, b]))
        self.assertEqual(sorted(results), ["DUPLICATE", "RECORDED"])
        self.assertEqual(self.journal.audit()["total_intents"], 1)

    def test_distinct_python_process_sees_persisted_reservation_and_refuses_repeat(self):
        self.journal.record_intent(intent())
        self.journal.reserve_simulated_attempt(_OID)
        code = (
            "import json,sys;"
            "from pathlib import Path;"
            "from bubus.order_journal import OfflineOrderJournal,OrderJournalError;"
            "j=OfflineOrderJournal(Path(sys.argv[1]));"
            "e=j.get(sys.argv[2]);"
            "assert e['attempt_count']==1 and e['uncertain_after_restart'];"
            "assert e['can_retry_automatically'] is False;"
            "try_attempt=lambda: j.reserve_simulated_attempt(sys.argv[2]);"
            "try_attempt()"
        )
        run = subprocess.run([sys.executable, "-c", code, str(self.path), _OID],
                             env=os.environ.copy(), capture_output=True,
                             text=True, timeout=15)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("already reserved", run.stderr)
        self.assertEqual(self.journal.get(_OID)["event_count"], 2)

    def test_abrupt_worker_exit_does_not_release_reserved_oid(self):
        self.journal.record_intent(intent())
        code = (
            "import os,sys;"
            "from pathlib import Path;"
            "from bubus.order_journal import OfflineOrderJournal;"
            "j=OfflineOrderJournal(Path(sys.argv[1]));"
            "j.reserve_simulated_attempt(sys.argv[2]);"
            "os._exit(7)"
        )
        run = subprocess.run([sys.executable, "-c", code, str(self.path), _OID],
                             env=os.environ.copy(), capture_output=True, timeout=15)
        self.assertEqual(run.returncode, 7)
        on_restart = self.restart().get(_OID)
        self.assertEqual(on_restart["attempt_count"], 1)
        self.assertTrue(on_restart["uncertain_after_restart"])
        with self.assertRaises(OrderJournalError):
            self.restart().reserve_simulated_attempt(_OID)

    def test_atomic_attempt_survives_crash_and_blocks_resubmission(self):
        self.journal.record_intent(intent())
        reserved = self.journal.reserve_simulated_attempt(_OID)
        self.assertEqual(reserved["attempt_count"], 1)
        self.assertTrue(reserved["uncertain_after_restart"])
        self.assertTrue(reserved["needs_manual_reconciliation"])
        recovered = self.restart().get(_OID)
        self.assertEqual(recovered["journal_status"], "BLOCKED_UNCERTAIN")
        self.assertFalse(recovered["can_retry_automatically"])
        with self.assertRaisesRegex(OrderJournalError, "already reserved"):
            self.restart().reserve_simulated_attempt(_OID)
        self.assertEqual(self.journal.get(_OID)["attempt_count"], 1)
        self.assertEqual(self.journal.audit()["uncertain_count"], 1)

    def test_uncertain_network_and_failed_readback_remain_blocked(self):
        self.journal.record_intent(intent())
        self.journal.reserve_simulated_attempt(_OID)
        self.journal.record_uncertain_response(_OID)
        failed = self.journal.record_simulated_readback_failure(_OID)
        self.assertEqual(failed["journal_status"], "BLOCKED_UNCERTAIN")
        self.assertTrue(failed["uncertain_after_restart"])
        self.assertTrue(self.restart().get(_OID)["needs_manual_reconciliation"])
        self.assertFalse(self.restart().audit()["can_retry_automatically"])

    def test_ack_is_not_fill_and_partial_cancel_remains_manual(self):
        self.journal.record_intent(intent())
        self.journal.reserve_simulated_attempt(_OID)
        acked = self.journal.record_simulated_ack(_OID, "987")
        self.assertEqual(acked["observed_status"], None)
        self.assertEqual(acked["journal_status"], "BLOCKED_UNCERTAIN")
        cancelled = self.journal.record_simulated_readback(
            _OID, order_id="987", status="CANCELED",
            cumulative_quantity="0.04")
        self.assertEqual(cancelled["observed_status"], "CANCELED")
        self.assertEqual(cancelled["journal_status"], "BLOCKED_INCOMPLETE")
        self.assertTrue(cancelled["needs_manual_reconciliation"])
        self.assertEqual(cancelled["attempt_count"], 1)
        self.assertFalse(cancelled["can_retry_automatically"])
        self.assertEqual(cancelled, self.restart().get(_OID))

    def test_bad_readback_rolls_back_transaction_and_is_not_persisted(self):
        self.journal.record_intent(intent())
        self.journal.reserve_simulated_attempt(_OID)
        before = self.journal.get(_OID)
        for values in (
            dict(order_id="1", status="FILLED", cumulative_quantity="NaN"),
            dict(order_id="1", status="FILLED", cumulative_quantity="-1"),
            dict(order_id="1", status="ACTIVE", cumulative_quantity="Infinity"),
            dict(order_id="bad", status="ACTIVE", cumulative_quantity="0"),
        ):
            with self.subTest(values=values):
                with self.assertRaises((ValueError, TypeError, OrderJournalError)):
                    self.journal.record_simulated_readback(_OID, **values)
                self.assertEqual(self.journal.get(_OID), before)

    def test_duplicate_attempt_rejected_after_timeout_and_restart(self):
        self.journal.record_intent(intent())
        self.journal.reserve_simulated_attempt(_OID)
        self.journal.record_uncertain_response(_OID)
        with self.assertRaises(OrderJournalError):
            self.restart().reserve_simulated_attempt(_OID)
        self.assertEqual(self.journal.audit()["uncertain_count"], 1)

    def test_untrusted_contradictory_readbacks_are_visible_and_always_blocked(self):
        self.journal.record_intent(intent())
        self.journal.reserve_simulated_attempt(_OID)
        self.journal.record_simulated_ack(_OID, "111")
        wrong_id = self.journal.record_simulated_readback(
            _OID, order_id="222", status="FILLED", cumulative_quantity="0.1")
        self.assertEqual(wrong_id["journal_status"], "BLOCKED_CONTRADICTORY")
        self.assertIn("ACK_READBACK_ORDER_ID_MISMATCH", wrong_id["reason_codes"])
        self.assertFalse(wrong_id["can_submit"])

    def test_empty_journal_and_many_intents_always_blocked(self):
        empty = self.journal.audit()
        self.assertEqual(empty["total_intents"], 0)
        self.assertTrue(empty["all_entries_verified"])
        self.assertEqual(empty["status"], "BLOCKED_SIMULATION_ONLY")
        for number in range(1, 26):
            oid = str(uuid.UUID(int=number))
            self.journal.record_intent(intent(oid))
        limited = self.restart().audit(limit=2)
        self.assertEqual(limited["total_intents"], 25)
        self.assertEqual(len(limited["entries"]), 2)
        self.assertFalse(limited["orders_enabled"])
        self.assertEqual(limited, self.restart().audit(limit=2))

    def test_integrity_checks_reject_rewritten_event_and_head(self):
        self.journal.record_intent(intent())
        db = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE offline_order_events SET payload_json='{}'")
            db.execute("DROP TRIGGER offline_events_no_update")
            db.execute("UPDATE offline_order_events SET event_sha256=?", ("a" * 64,))
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(OrderJournalError, "hash chain"):
            self.restart().audit()

    def test_journal_rejects_tampered_immutable_intent_and_head(self):
        self.journal.record_intent(intent())
        db = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE offline_order_intents SET client_oid='different'")
            db.execute("UPDATE offline_order_intents SET event_head_sha256=?", ("b" * 64,))
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(OrderJournalError, "head"):
            self.restart().get(_OID)

    def test_attempt_before_intent_or_readback_before_attempt_is_refused(self):
        with self.assertRaises(OrderJournalError):
            self.journal.reserve_simulated_attempt(_OID)
        self.journal.record_intent(intent())
        with self.assertRaises(OrderJournalError):
            self.journal.record_simulated_ack(_OID, "123")
        with self.assertRaises(OrderJournalError):
            self.journal.record_simulated_readback(
                _OID, order_id="123", status="FILLED", cumulative_quantity="0.1")
        self.assertEqual(self.journal.get(_OID)["event_count"], 1)

    def test_schema_unknown_is_refused_without_silent_migration(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA user_version=999")
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(OrderJournalError, "schema"):
            self.restart()


def try_record(journal):
    try:
        journal.record_intent(intent())
        return "RECORDED"
    except OrderJournalError:
        return "DUPLICATE"


class OfflineJournalApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(0, data_dir=self.tmp.name,
            cryptocom_market_overview=FakeCryptoOverview(),
            coingecko_market_overview=FakeCoinGeckoOverview())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def test_empty_api_and_restart_persistence_are_read_only(self):
        with urlopen(self.url + "/api/desk/order-journal") as response:
            report = json.load(response)["journal"]
        self.assertEqual(report["total_intents"], 0)
        self.assertFalse(report["orders_enabled"])
        self.assertFalse(report["can_submit"])
        self.assertEqual(report["authority_status"], "SYNTHETIC_ORDER_JOURNAL_ONLY")
        self.server.offline_order_journal.record_intent(intent())
        with urlopen(self.url + "/api/desk/order-journal") as response:
            report = json.load(response)["journal"]
        self.assertEqual(report["total_intents"], 1)
        self.assertFalse(report["can_retry_automatically"])

    def test_frontend_does_not_expose_synthetic_order_journal(self):
        with urlopen(self.url + "/assets/desk.js") as response:
            script = response.read().decode("utf-8")
        self.assertNotIn("Journal durable des essais hors ligne", script)
        self.assertNotIn("loadOfflineOrderJournal()", script)
        self.assertNotIn("/api/desk/order-journal", script)
        self.assertNotIn("BLOCKED_SIMULATION_ONLY", script)
        self.assertNotIn("create_limit_order(", script)

    def test_post_and_foreign_origin_are_rejected_without_order(self):
        req = Request(self.url + "/api/desk/order-journal",
                      headers={"Origin": "https://foreign.example"})
        with self.assertRaises(HTTPError) as denied:
            urlopen(req)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        req = Request(self.url + "/api/desk/order-journal",
                      data=b'{"client_oid":"unsafe","enable_orders":true}',
                      method="POST", headers={"Origin": self.url,
                                               "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as denied:
            urlopen(req)
        self.assertIn(denied.exception.code, {400, 403, 404, 405, 409, 501})
        denied.exception.close()
        with urlopen(self.url + "/api/desk/state") as response:
            trading = json.load(response)["trading"]
        self.assertFalse(trading["orders_enabled"])
        self.assertFalse(trading["armed"])

    def test_corrupt_journal_triggers_http_503_without_partial_entries(self):
        self.server.offline_order_journal.record_intent(intent())
        db = sqlite3.connect(Path(self.tmp.name) / "offline-order-journal.sqlite3")
        try:
            db.execute("UPDATE offline_order_intents SET event_head_sha256=?", ("c" * 64,))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(HTTPError) as failed:
            urlopen(self.url + "/api/desk/order-journal")
        self.assertEqual(failed.exception.code, 503)
        failed.exception.close()


if __name__ == "__main__":
    unittest.main()
