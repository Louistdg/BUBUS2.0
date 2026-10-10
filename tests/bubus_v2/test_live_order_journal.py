from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from bubus.live_order_journal import LiveOrderJournal, LiveOrderJournalError
from invest_ai.execution.cryptocom_spot import CryptoComOrderStatus
from tests.execution.test_cryptocom_spot import CLIENT_OID, intent


class LiveOrderJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "live-orders.sqlite3"
        self.journal = LiveOrderJournal(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_intent_reservation_and_authorization_attempt_ack_are_durable(self):
        order = intent()
        self.journal.reserve_intent(order)
        self.journal.record_authorization(CLIENT_OID, risk_receipt_id="risk-1",
            policy_revision="policy-1", control_epoch=7, human_approval_id="owner-1")
        self.journal.record_submission_attempt(CLIENT_OID, order.canonical_sha256())
        self.journal.record_acknowledgement(CLIENT_OID, order_id="123", echoed_client_oid=CLIENT_OID)

        restarted = LiveOrderJournal(self.path)
        report = restarted.audit(CLIENT_OID)
        self.assertEqual(report["authority_status"], "LIVE_ORDER_JOURNAL_EVIDENCE_ONLY")
        self.assertFalse(report["orders_enabled"])
        self.assertEqual(report["intents"][0]["state"], "ACKNOWLEDGED")
        self.assertEqual(report["intents"][0]["event_count"], 4)

    def test_duplicate_reservation_is_refused_even_after_restart(self):
        self.journal.reserve_intent(intent())
        restarted = LiveOrderJournal(self.path)
        with self.assertRaisesRegex(LiveOrderJournalError, "client_oid already exists"):
            restarted.reserve_intent(intent())

    def test_uncertain_submission_cannot_be_reauthorized_or_retried(self):
        order = intent()
        self.journal.reserve_intent(order)
        self.journal.record_authorization(CLIENT_OID, risk_receipt_id="risk-1",
            policy_revision="policy-1", control_epoch=7, human_approval_id="owner-1")
        self.journal.record_submission_attempt(CLIENT_OID, order.canonical_sha256())
        self.journal.record_submission_uncertain(CLIENT_OID, "TRANSPORT_ERROR")
        with self.assertRaisesRegex(LiveOrderJournalError, "does not allow"):
            self.journal.record_submission_attempt(CLIENT_OID, order.canonical_sha256())
        self.assertEqual(self.journal.audit(CLIENT_OID)["intents"][0]["state"], "UNCERTAIN")

    def test_gate_refusal_is_persisted_and_terminal_for_that_order_id(self):
        self.journal.reserve_intent(intent())
        self.journal.record_authorization_rejected(CLIENT_OID, "GATE_REFUSED")
        report = self.journal.audit(CLIENT_OID)
        self.assertEqual(report["intents"][0]["state"], "REJECTED")
        with self.assertRaises(LiveOrderJournalError):
            self.journal.record_authorization(CLIENT_OID, risk_receipt_id="risk-1",
                policy_revision="policy-1", control_epoch=7, human_approval_id="owner-1")

    def test_database_status_tampering_is_detected_against_event_history(self):
        self.journal.reserve_intent(intent())
        db = sqlite3.connect(self.path)
        try:
            db.execute("UPDATE live_order_intents SET state='ACKNOWLEDGED' WHERE client_oid=?", (CLIENT_OID,))
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(LiveOrderJournalError, "differs from its event history"):
            self.journal.audit(CLIENT_OID)

    def test_transition_refuses_to_extend_a_tampered_state(self):
        self.journal.reserve_intent(intent())
        db = sqlite3.connect(self.path)
        try:
            db.execute("UPDATE live_order_intents SET state='ACKNOWLEDGED' WHERE client_oid=?", (CLIENT_OID,))
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(LiveOrderJournalError, "state differs from its event history"):
            self.journal.record_authorization(CLIENT_OID, risk_receipt_id="risk-1",
                policy_revision="policy-1", control_epoch=7, human_approval_id="owner-1")

    def test_readback_resolves_exchange_identity_but_never_authorizes_or_retries(self):
        order = intent()
        self.journal.reserve_intent(order)
        self.journal.record_authorization(CLIENT_OID, risk_receipt_id="risk-1",
            policy_revision="policy-1", control_epoch=7, human_approval_id="owner-1")
        self.journal.record_submission_attempt(CLIENT_OID, order.canonical_sha256())
        self.journal.record_submission_uncertain(CLIENT_OID, "TRANSPORT_ERROR")
        self.journal.record_order_readback(CryptoComOrderStatus(
            order_id="123", client_oid=CLIENT_OID, status="ACTIVE",
            executed_quantity=Decimal("0"), executed_value=Decimal("0"), fees=Decimal("0"),
            updated_at=datetime(2026, 10, 10, tzinfo=timezone.utc), fee_currency=None,
        ))
        report = self.journal.audit(CLIENT_OID)
        self.assertEqual(report["intents"][0]["state"], "UNCERTAIN")
        self.assertEqual(report["intents"][0]["last_event"], "ORDER_READBACK_OBSERVED")
        self.assertFalse(report["orders_enabled"])
        with self.assertRaises(LiveOrderJournalError):
            self.journal.record_submission_attempt(CLIENT_OID, order.canonical_sha256())

    def test_readback_rejects_order_id_conflict_and_execution_regression(self):
        order = intent()
        self.journal.reserve_intent(order)
        self.journal.record_authorization(CLIENT_OID, risk_receipt_id="risk-1",
            policy_revision="policy-1", control_epoch=7, human_approval_id="owner-1")
        self.journal.record_submission_attempt(CLIENT_OID, order.canonical_sha256())
        self.journal.record_acknowledgement(CLIENT_OID, order_id="123", echoed_client_oid=CLIENT_OID)
        now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        with self.assertRaisesRegex(LiveOrderJournalError, "conflicts"):
            self.journal.record_order_readback(CryptoComOrderStatus(
                "124", CLIENT_OID, "ACTIVE", Decimal("0"), Decimal("0"), Decimal("0"), now))
        self.journal.record_order_readback(CryptoComOrderStatus(
            "123", CLIENT_OID, "ACTIVE", Decimal("0.00008"), Decimal("4.8"), Decimal("0.01"), now))
        with self.assertRaisesRegex(LiveOrderJournalError, "regressed"):
            self.journal.record_order_readback(CryptoComOrderStatus(
                "123", CLIENT_OID, "ACTIVE", Decimal("0.00004"), Decimal("2.4"), Decimal("0.005"), now))


if __name__ == "__main__":
    unittest.main()
