"""Atomicity tests for the offline Risk + durable order rehearsal."""
from __future__ import annotations

import tempfile
import sqlite3
import threading
import json
import base64
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from bubus.offline_trade_transaction import (
    OfflineTradeTransaction,
    OfflineTradeTransactionError,
)
from bubus.order_journal import OrderJournalError
from bubus.server import make_server
from bubus.cloud_preview import AuthenticatedCloudPreviewHandler
from invest_ai.execution.cryptocom_spot import CryptoComSpotLimitOrder
from tests.assistant.test_market_dashboard import FakeCoinGeckoOverview, FakeCryptoOverview


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def make_intent(oid: str, *, price: str = "40", quantity: str = "1"):
    return CryptoComSpotLimitOrder(
        client_oid=oid, instrument_name="BTC_USDT", side="BUY",
        price=Decimal(price), quantity=Decimal(quantity),
    )


class OfflineTradeTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "atomic-offline-rehearsal.sqlite3"
        self.clock = [NOW]
        self.coordinator = OfflineTradeTransaction(self.path, clock=lambda: self.clock[0])
        self.coordinator.risk.set_synthetic_budget(
            quote_currency="USDT", capital_limit="100", max_order_notional="70",
            max_daily_loss="20", observed_daily_loss="2", fee_cushion="0.01",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def approve(self, intent, approval_id=None):
        return self.coordinator.risk.record_synthetic_human_approval(
            intent, approval_id=approval_id or str(uuid.uuid4()),
            approver_label="SYNTHETIC_REVIEWER", expires_at=NOW + timedelta(minutes=5),
        )

    def test_intent_reservation_consumption_and_proof_share_one_transaction(self):
        intent = make_intent(str(uuid.uuid4()))
        self.approve(intent, "00000000-0000-0000-0000-000000001001")
        report = self.coordinator.record_and_reserve(
            approval_id="00000000-0000-0000-0000-000000001001", intent=intent,
        )
        self.assertTrue(report["single_sqlite_database"])
        self.assertTrue(report["atomic_order_risk_transaction"])
        self.assertFalse(report["orders_enabled"])
        self.assertEqual(report["verified_linked_transactions"], 1)
        risk = self.coordinator.risk.audit()
        order = self.coordinator.orders.get(intent.client_oid)
        self.assertEqual(risk["reserved_capital"], "40.40000000")
        self.assertEqual(risk["consumed_approvals"], 1)
        self.assertEqual(order["event_count"], 2)
        self.assertEqual(order["risk_reservation"]["approval_id"],
                         "00000000-0000-0000-0000-000000001001")
        self.assertEqual(len(order["risk_reservation"]["transaction_sha256"]), 64)

    def test_failure_after_all_writes_rolls_back_intent_receipt_and_reservation(self):
        intent = make_intent(str(uuid.uuid4()))
        aid = "00000000-0000-0000-0000-000000001002"
        self.approve(intent, aid)
        append = self.coordinator.orders._append

        def fail_after_risk_event(db, row, kind, payload, at):
            append(db, row, kind, payload, at)
            if kind == "SIMULATED_RISK_RESERVED":
                raise RuntimeError("simulated process failure before commit")

        self.coordinator.orders._append = fail_after_risk_event
        with self.assertRaisesRegex(RuntimeError, "before commit"):
            self.coordinator.record_and_reserve(approval_id=aid, intent=intent)
        self.coordinator.orders._append = append
        self.assertEqual(self.coordinator.risk.audit()["pending_approvals"], 1)
        self.assertEqual(self.coordinator.risk.audit()["reserved_capital"], "0")
        self.assertEqual(self.coordinator.orders.audit()["total_intents"], 0)
        self.assertEqual(self.coordinator.audit()["verified_linked_transactions"], 0)

    def test_concurrent_orders_cannot_overreserve_shared_budget(self):
        a = make_intent(str(uuid.uuid4()), price="60")
        b = make_intent(str(uuid.uuid4()), price="50")
        aid_a, aid_b = "00000000-0000-0000-0000-000000001003", "00000000-0000-0000-0000-000000001004"
        self.approve(a, aid_a)
        self.approve(b, aid_b)
        barrier = threading.Barrier(3)

        def reserve(coordinator, aid, order):
            barrier.wait()
            try:
                coordinator.record_and_reserve(approval_id=aid, intent=order)
                return "RESERVED"
            except OfflineTradeTransactionError:
                return "BLOCKED"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(reserve, OfflineTradeTransaction(self.path, clock=lambda: self.clock[0]), aid_a, a)
            second = pool.submit(reserve, OfflineTradeTransaction(self.path, clock=lambda: self.clock[0]), aid_b, b)
            barrier.wait()
            results = sorted([first.result(timeout=10), second.result(timeout=10)])
        self.assertEqual(results, ["BLOCKED", "RESERVED"])
        risk = self.coordinator.risk.audit()
        self.assertEqual(risk["consumed_approvals"], 1)
        self.assertLess(Decimal(risk["reserved_capital"]), Decimal("100"))
        self.assertEqual(self.coordinator.audit()["verified_linked_transactions"], 1)

    def test_corrupt_cross_ledger_link_is_refused(self):
        intent = make_intent(str(uuid.uuid4()))
        aid = "00000000-0000-0000-0000-000000001005"
        self.approve(intent, aid)
        self.coordinator.record_and_reserve(approval_id=aid, intent=intent)
        # Break the event payload while leaving its digest unchanged. The full
        # journal audit must reject it before the cross-ledger report is built.
        db = sqlite3.connect(self.path)
        try:
            db.execute("DROP TRIGGER offline_events_no_update")
            db.execute("UPDATE offline_order_events SET payload_json='{}' WHERE event_type='SIMULATED_RISK_RESERVED'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(OrderJournalError):
            self.coordinator.audit()


class OfflineTradeTransactionRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(
            0, data_dir=self.tmp.name,
            cryptocom_market_overview=FakeCryptoOverview(),
            coingecko_market_overview=FakeCoinGeckoOverview(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def test_read_only_route_reports_no_live_authority_and_rejects_writes(self):
        with urlopen(self.url + "/api/desk/offline-trade-transaction") as response:
            report = json.load(response)["transaction"]
        self.assertEqual(report["status"], "BLOCKED_SIMULATION_ONLY")
        self.assertTrue(report["single_sqlite_database"])
        self.assertFalse(report["orders_enabled"])
        self.assertFalse(report["connected_to_live_adapter"])
        request = Request(
            self.url + "/api/desk/offline-trade-transaction",
            data=b'{"submit":true}', method="POST",
            headers={"Origin": self.url, "Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertIn(error.exception.code, {400, 403, 404, 405, 501})
        error.exception.close()


class AuthenticatedCloudPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "BUBUS_PREVIEW_USERNAME": "owner",
            "BUBUS_PREVIEW_PASSWORD": "P" * 32,
        })
        self.env.start()
        self.server = make_server(
            0, data_dir=self.tmp.name, bind_host="127.0.0.1",
            handler_class=AuthenticatedCloudPreviewHandler,
            cryptocom_market_overview=FakeCryptoOverview(),
            coingecko_market_overview=FakeCoinGeckoOverview(),
            cryptocom_instrument_catalog=FakeInstrumentCatalog(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.authorization = "Basic " + base64.b64encode(b"owner:" + b"P" * 32).decode()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.env.stop()
        self.tmp.cleanup()

    def test_preview_authenticates_and_exposes_only_read_only_routes(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + "/")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        with urlopen(Request(self.url + "/", headers={"Authorization": self.authorization})) as response:
            html = response.read()
            self.assertIn(b"BUBUS2.0", html)
            self.assertIn(b"APER\xc3\x87U CLOUD PROT\xc3\x89G\xc3\x89", html)
        with urlopen(self.url + "/healthz") as response:
            self.assertEqual(response.read(), b"ok")
        with urlopen(Request(self.url + "/api/desk/state", headers={"Authorization": self.authorization})) as response:
            state = json.load(response)
        self.assertFalse(state["trading"]["orders_enabled"])
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.url + "/api/desk/live-signal?symbol=BTC_USDT&timeframe=5m",
                            headers={"Authorization": self.authorization}))
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        with urlopen(Request(self.url + "/api/market-data/crypto-overview",
                             headers={"Authorization": self.authorization})) as response:
            markets = json.load(response)
        self.assertEqual(markets["provider"], "Crypto.com Exchange")
        self.assertEqual(markets["markets"][0]["symbol"], "BTC_USDT")
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.url + "/api/desk/portfolio", headers={"Authorization": self.authorization}))
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.url + "/api/desk/watchlist", data=b"{}", method="POST",
                            headers={"Authorization": self.authorization, "Content-Type": "application/json"}))
        self.assertEqual(error.exception.code, 405)
        error.exception.close()


class FakeInstrumentCatalog:
    def get_spot_catalog(self):
        return {}


if __name__ == "__main__":
    unittest.main()
