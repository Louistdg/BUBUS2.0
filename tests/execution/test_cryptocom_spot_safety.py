"""Safety regression tests for the disconnected Crypto.com Spot adapter.

Every gate, verifier and transport used here is a local fake. No credentials,
broker connection, real order submission or account state is involved.
"""
from __future__ import annotations

import threading
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from bubus.live_order_journal import LiveOrderJournal
from invest_ai.data.cryptocom_instruments import CryptoComInstrumentCatalog
from invest_ai.execution.cryptocom_spot import (
    CryptoComSpotOrderAdapter, CryptoComSpotOrderError,
)
from tests.execution.test_cryptocom_spot import (
    API_KEY, API_SECRET, CLIENT_OID, NOW,
    FakeGate, FakeSpotVerifier, FakeTransport, intent,
)

METHOD_CREATE = "private/create-order"
METHOD_DETAIL = "private/get-order-detail"
_ADAPTER_TEMP_DIRS = []


def _cleanup_adapter_temp_dirs():
    for temp in _ADAPTER_TEMP_DIRS:
        temp.cleanup()


unittest.addModuleCleanup(_cleanup_adapter_temp_dirs)


def acknowledge(code=0, order_id="123", client_oid=CLIENT_OID):
    return {"method": METHOD_CREATE, "code": code,
            "result": {"order_id": order_id, "client_oid": client_oid}}


def detail(status="ACTIVE", *, order_id="123", client_oid=CLIENT_OID,
           quantity="0", value="0", fee="0", **kwargs):
    return {"method": METHOD_DETAIL, "code": 0,
            "result": {"order_id": order_id, "client_oid": client_oid,
                       "status": status, "cumulative_quantity": quantity,
                       "cumulative_value": value, "cumulative_fee": fee, **kwargs}}


def adapter_for(transport, *, gate=None, journal=None):
    temp = None
    if journal is None:
        temp = tempfile.TemporaryDirectory()
        _ADAPTER_TEMP_DIRS.append(temp)
        journal = LiveOrderJournal(Path(temp.name) / "live-orders.sqlite3")
    adapter = CryptoComSpotOrderAdapter(
        authorization_gate=gate, instrument_verifier=FakeSpotVerifier(),
        order_journal=journal,
        transport=transport, clock=lambda: NOW,
    )
    adapter._test_temp_dir = temp
    return adapter


class FailingTransport(FakeTransport):
    def request(self, method, api_key, api_secret, params):
        self.calls.append((method, api_key, api_secret, dict(params)))
        raise TimeoutError("synthetic timeout after potential remote submission")


class PublicResponse:
    def __init__(self, payload): self.payload = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def read(self, amount=-1): return self.payload[:amount]


def minimum_order_catalog():
    rows = [
        {"symbol": "BTC_USDT", "inst_type": "CCY_PAIR", "base_ccy": "BTC", "quote_ccy": "USDT",
         "price_tick_size": "0.01", "qty_tick_size": "0.00001", "tradable": True},
        {"symbol": "USDT_USD", "inst_type": "CCY_PAIR", "base_ccy": "USDT", "quote_ccy": "USD",
         "price_tick_size": "0.00001", "qty_tick_size": "0.1", "tradable": True},
    ]
    def opener(request, timeout):
        if urlsplit(request.full_url).path.endswith("/get-instruments"):
            return PublicResponse({"code": 0, "result": {"data": rows}})
        symbol = urlsplit(request.full_url).query.split("=", 1)[1]
        bid, ask = (("0.9999", "1.0001") if symbol == "USDT_USD"
                    else ("59999.99", "60000.01"))
        return PublicResponse({"code": 0, "result": {"data": [{"i": symbol, "b": bid,
            "k": ask, "t": int(time.time() * 1000)}]}})
    return CryptoComInstrumentCatalog(opener=opener)


class SpotOrderHardeningTests(unittest.TestCase):
    def test_below_exchange_minimum_stops_before_journal_gate_and_transport(self):
        gate, transport = FakeGate(), FakeTransport()
        with tempfile.TemporaryDirectory() as temp:
            journal = LiveOrderJournal(Path(temp) / "live-orders.sqlite3", clock=lambda: NOW)
            adapter = CryptoComSpotOrderAdapter(
                authorization_gate=gate, instrument_verifier=minimum_order_catalog(),
                order_journal=journal, transport=transport, clock=lambda: NOW)
            small = intent(quantity=Decimal("0.00001"))
            with self.assertRaisesRegex(CryptoComSpotOrderError, "tradable SPOT instrument"):
                adapter.create_limit_order(small, api_key=API_KEY, api_secret=API_SECRET)
            self.assertFalse(journal.contains_intent(CLIENT_OID))
        self.assertEqual(gate.calls, [])
        self.assertEqual(transport.calls, [])

    def test_duplicate_oid_refused_before_gate_and_transport_even_after_ack(self):
        gate, transport = FakeGate(), FakeTransport([acknowledge()])
        adapter = adapter_for(transport, gate=gate)
        first = adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
        self.assertEqual(first.status, "ACKNOWLEDGED_NOT_FILLED")
        with self.assertRaisesRegex(CryptoComSpotOrderError, "already attempted"):
            adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
        self.assertEqual(len(gate.calls), 1)
        self.assertEqual(len(transport.calls), 1)

    def test_timeout_never_resends_same_oid(self):
        gate, transport = FakeGate(), FailingTransport()
        adapter = adapter_for(transport, gate=gate)
        with self.assertRaisesRegex(CryptoComSpotOrderError, "response unknown"):
            adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
        with self.assertRaisesRegex(CryptoComSpotOrderError, "already attempted"):
            adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(gate.calls), 1)

    def test_gate_failure_also_remains_reserved_for_review(self):
        class DeniedGate(FakeGate):
            def consume(self, order, digest):
                self.calls.append((order, digest))
                raise PermissionError("synthetic refusal")
        gate, transport = DeniedGate(), FakeTransport()
        adapter = adapter_for(transport, gate=gate)
        with self.assertRaisesRegex(CryptoComSpotOrderError, "gate refused"):
            adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
        with self.assertRaisesRegex(CryptoComSpotOrderError, "already attempted"):
            adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
        self.assertEqual(len(gate.calls), 1)
        self.assertEqual(transport.calls, [])

    def test_concurrent_duplicate_oid_allows_only_one_fake_transport_request(self):
        gate, transport = FakeGate(), FakeTransport([acknowledge()])
        adapter = adapter_for(transport, gate=gate)
        barrier = threading.Barrier(3)
        def invoke():
            barrier.wait()
            try:
                return adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
            except CryptoComSpotOrderError as e:
                return e
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(invoke) for _ in range(2)]
            barrier.wait()
            results = [f.result(timeout=5) for f in futures]
        self.assertEqual(len(gate.calls), 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sum(isinstance(v, CryptoComSpotOrderError) for v in results), 1)

    def test_bad_acknowledgement_never_becomes_success(self):
        for response in (
            acknowledge(code=0.0), acknowledge(code=False), acknowledge(order_id="bad"),
            acknowledge(order_id="0"), acknowledge(order_id=None),
            acknowledge(order_id=True), acknowledge(client_oid="changed"),
            {"method": METHOD_CREATE, "code": 0, "result": None},
            [],
        ):
            with self.subTest(response=response):
                adapter = adapter_for(FakeTransport([response]), gate=FakeGate())
                with self.assertRaises(CryptoComSpotOrderError):
                    adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)

    def test_order_detail_accepts_only_known_exchange_statuses(self):
        for status in ("PENDING", "NEW", "ACTIVE", "FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            with self.subTest(status=status):
                qty = "0.0001" if status == "FILLED" else "0"
                adapter = adapter_for(FakeTransport([detail(status, quantity=qty)]))
                result = adapter.get_order_detail(
                    api_key=API_KEY, api_secret=API_SECRET, client_oid=CLIENT_OID)
                self.assertEqual(result.status, status)
        for status in ("PARTIALLY_FILLED", "UNKNOWN", "", None, True, "filled"):
            with self.subTest(status=status):
                adapter = adapter_for(FakeTransport([detail(status)]))
                with self.assertRaises(CryptoComSpotOrderError):
                    adapter.get_order_detail(
                        api_key=API_KEY, api_secret=API_SECRET, client_oid=CLIENT_OID)

    def test_missing_or_changed_order_identity_is_never_reconciled(self):
        for response in (
            detail(order_id=None), detail(order_id="abc"), detail(order_id="0"),
            detail(order_id=True), detail(order_id="123", client_oid="other"),
            detail(order_id="123", client_oid=""),
        ):
            with self.subTest(response=response):
                adapter = adapter_for(FakeTransport([response]))
                with self.assertRaises(CryptoComSpotOrderError):
                    adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET,
                                             client_oid=CLIENT_OID)

    def test_order_detail_rejects_corrupt_quantity_fee_and_time(self):
        for field, bad in (
            ("cumulative_quantity", "NaN"), ("cumulative_quantity", "Infinity"),
            ("cumulative_quantity", "-1"), ("cumulative_quantity", "1e101"),
            ("cumulative_value", "9" * 100), ("cumulative_fee", -1),
            ("update_time", True), ("update_time", "n/a"),
            ("update_time", -123),
            ("fee_instrument_name", "???"), ("fee_instrument_name", 22),
        ):
            response = detail()
            response["result"][field] = bad
            with self.subTest(field=field, bad=bad):
                adapter = adapter_for(FakeTransport([response]))
                with self.assertRaises(CryptoComSpotOrderError):
                    adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET,
                                             client_oid=CLIENT_OID)

    def test_filled_with_zero_quantity_refused_and_partial_cancel_retained(self):
        adapter = adapter_for(FakeTransport([detail("FILLED", quantity="0")]))
        with self.assertRaises(CryptoComSpotOrderError):
            adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET,
                                     client_oid=CLIENT_OID)
        adapter = adapter_for(FakeTransport([detail("CANCELED", quantity="0.00008",
                                                  fee="0.001", fee_instrument_name="USDT",
                                                  update_time="1791579599000")]))
        result = adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET,
                                          client_oid=CLIENT_OID)
        self.assertEqual(result.status, "CANCELED")
        self.assertEqual(result.executed_quantity, Decimal("0.00008"))
        self.assertEqual(result.fee_currency, "USDT")
        self.assertIsInstance(result.updated_at, datetime)

    def test_future_provider_update_and_invalid_local_clock_are_rejected(self):
        future_ms = int((NOW.timestamp() + 20) * 1000)
        adapter = adapter_for(FakeTransport([detail("ACTIVE", update_time=future_ms)]))
        with self.assertRaisesRegex(CryptoComSpotOrderError, "future"):
            adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET,
                                     client_oid=CLIENT_OID)
        adapter = CryptoComSpotOrderAdapter(
            transport=FakeTransport([detail("ACTIVE", update_time=future_ms)]),
            clock=lambda: NOW.replace(tzinfo=None))
        with self.assertRaisesRegex(CryptoComSpotOrderError, "UTC clock"):
            adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET,
                                     client_oid=CLIENT_OID)

    def test_transport_failure_during_readback_is_not_proof_of_nonexecution(self):
        transport = FailingTransport()
        adapter = adapter_for(transport)
        with self.assertRaisesRegex(CryptoComSpotOrderError, "not proof"):
            adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET,
                                     client_oid=CLIENT_OID)
        self.assertEqual(len(transport.calls), 1)

    def test_missing_fee_currency_is_explicitly_unknown(self):
        adapter = adapter_for(FakeTransport([detail("ACTIVE", fee="0.02")]))
        result = adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET,
                                          client_oid=CLIENT_OID)
        self.assertIsNone(result.fee_currency)

    def test_adapter_persists_matching_exchange_readback_after_unknown_submission(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        journal = LiveOrderJournal(Path(temp.name) / "live-orders.sqlite3")
        order = intent()
        journal.reserve_intent(order)
        journal.record_authorization(CLIENT_OID, risk_receipt_id="risk-1",
            policy_revision="policy-1", control_epoch=1, human_approval_id="owner-1")
        journal.record_submission_attempt(CLIENT_OID, order.canonical_sha256())
        journal.record_submission_uncertain(CLIENT_OID, "TRANSPORT_ERROR")
        adapter = adapter_for(FakeTransport([detail("ACTIVE")]), journal=journal)
        result = adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET, client_oid=CLIENT_OID)
        self.assertEqual(result.status, "ACTIVE")
        entry = journal.audit(CLIENT_OID)["intents"][0]
        self.assertEqual(entry["state"], "UNCERTAIN")
        self.assertEqual(entry["last_event"], "ORDER_READBACK_OBSERVED")

    def test_durable_journal_refuses_same_client_oid_after_adapter_restart(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "live-orders.sqlite3"
        first = adapter_for(FakeTransport([acknowledge()]), gate=FakeGate(),
                            journal=LiveOrderJournal(path))
        first.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
        restarted = adapter_for(FakeTransport(), gate=FakeGate(), journal=LiveOrderJournal(path))
        with self.assertRaisesRegex(CryptoComSpotOrderError, "durable order journal"):
            restarted.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
        self.assertEqual(restarted._transport.calls, [])

    def test_missing_durable_journal_refuses_before_gate_or_transport(self):
        adapter = CryptoComSpotOrderAdapter(
            authorization_gate=FakeGate(), instrument_verifier=FakeSpotVerifier(),
            transport=FakeTransport([acknowledge()]), clock=lambda: NOW,
        )
        with self.assertRaisesRegex(CryptoComSpotOrderError, "durable cross-restart"):
            adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
        self.assertEqual(adapter._gate.calls, [])
        self.assertEqual(adapter._transport.calls, [])


if __name__ == "__main__":
    unittest.main()
