from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from invest_ai.execution.cryptocom_spot import (
    CryptoComExchangePrivateTransport,
    CryptoComSpotLimitOrder,
    CryptoComSpotOrderAdapter,
    CryptoComSpotOrderError,
    OrderAuthorization,
)
from bubus.live_order_journal import LiveOrderJournal


NOW = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)
API_KEY = "test-read-write-key"
API_SECRET = "test-secret"
CLIENT_OID = "fa14b16b-6b53-4716-8e78-253ad1cfe600"


def intent(**overrides):
    values = {
        "client_oid": CLIENT_OID,
        "instrument_name": "BTC_USDT",
        "side": "BUY",
        "price": Decimal("60000.00"),
        "quantity": Decimal("0.0001"),
    }
    values.update(overrides)
    return CryptoComSpotLimitOrder(**values)


class FakeTransport:
    def __init__(self, responses=()):
        self.calls = []
        self.responses = list(responses)

    def request(self, method, api_key, api_secret, params):
        self.calls.append((method, api_key, api_secret, dict(params)))
        return self.responses.pop(0)


class FakeGate:
    def __init__(self, *, digest=None, expiry=None):
        self.calls = []
        self.digest = digest
        self.expiry = expiry or NOW + timedelta(seconds=30)

    def consume(self, order, digest):
        self.calls.append((order, digest))
        return OrderAuthorization(
            risk_receipt_id="risk-receipt-1", policy_revision="policy-sha256-1",
            control_epoch=7, human_approval_id="human-approval-1",
            intent_sha256=self.digest or digest, expires_at=self.expiry,
        )


class FakeSpotVerifier:
    def __init__(self, *, reject=False):
        self.calls = []
        self.reject = reject

    def assert_spot_limit_order(self, order):
        self.calls.append(order)
        if self.reject or not order.instrument_name.endswith("_USDT"):
            raise ValueError("not a currently verified Spot instrument")


class CryptoComSpotOrderTests(unittest.TestCase):
    def test_no_gate_refuses_before_any_transport_call(self):
        transport = FakeTransport()
        adapter = CryptoComSpotOrderAdapter(instrument_verifier=FakeSpotVerifier(),
                                             transport=transport, clock=lambda: NOW)
        with self.assertRaisesRegex(CryptoComSpotOrderError, "no trusted Risk/Policy/human-approval gate"):
            adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
        self.assertEqual(transport.calls, [])

    def test_only_canonical_spot_pairs_limit_decimal_and_uuid_are_accepted(self):
        for fields in (
            {"instrument_name": "BTCUSD-PERP"},
            {"instrument_name": "BTCUSD"},
            {"side": "SHORT"},
            {"price": 0.2},
            {"client_oid": "not-a-uuid"},
        ):
            with self.subTest(fields=fields), self.assertRaises(CryptoComSpotOrderError):
                intent(**fields)

    def test_create_order_uses_exact_approved_intent_and_fill_or_kill(self):
        order = intent()
        gate = FakeGate()
        verifier = FakeSpotVerifier()
        transport = FakeTransport([{
            "id": 1, "method": "private/create-order", "code": 0,
            "result": {"order_id": "987654321", "client_oid": CLIENT_OID},
        }])
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = LiveOrderJournal(Path(temp_dir) / "live-orders.sqlite3", clock=lambda: NOW)
            adapter = CryptoComSpotOrderAdapter(authorization_gate=gate, instrument_verifier=verifier,
                                                 order_journal=journal, transport=transport,
                                                 clock=lambda: NOW)
            ack = adapter.create_limit_order(order, api_key=API_KEY, api_secret=API_SECRET)
        self.assertEqual(len(gate.calls), 1)
        self.assertEqual(verifier.calls, [order])
        self.assertEqual(gate.calls[0][1], order.canonical_sha256())
        self.assertEqual(transport.calls, [("private/create-order", API_KEY, API_SECRET, {
            "instrument_name": "BTC_USDT", "side": "BUY", "type": "LIMIT",
            "price": "60000.00", "quantity": "0.0001", "client_oid": CLIENT_OID,
            "time_in_force": "FILL_OR_KILL",
        })])
        self.assertEqual(ack.order_id, "987654321")
        self.assertEqual(ack.status, "ACKNOWLEDGED_NOT_FILLED")
        self.assertEqual(ack.risk_receipt_id, "risk-receipt-1")
        self.assertEqual(ack.human_approval_id, "human-approval-1")

    def test_expired_or_mismatched_approval_never_reaches_transport(self):
        for gate in (
            FakeGate(expiry=NOW),
            FakeGate(digest="0" * 64),
        ):
            transport = FakeTransport()
            adapter = CryptoComSpotOrderAdapter(authorization_gate=gate,
                                                 instrument_verifier=FakeSpotVerifier(), transport=transport,
                                                 clock=lambda: NOW)
            with self.subTest(gate=gate), self.assertRaises(CryptoComSpotOrderError):
                adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
            self.assertEqual(transport.calls, [])

    def test_missing_gate_and_invalid_credential_never_reaches_transport(self):
        transport = FakeTransport()
        adapter = CryptoComSpotOrderAdapter(authorization_gate=FakeGate(),
                                             instrument_verifier=FakeSpotVerifier(), transport=transport,
                                             clock=lambda: NOW)
        with self.assertRaises(CryptoComSpotOrderError):
            adapter.create_limit_order(intent(), api_key=API_KEY, api_secret="")
        self.assertEqual(transport.calls, [])

    def test_order_status_is_read_back_and_never_inferred_from_ack(self):
        transport = FakeTransport([{
            "id": 2, "method": "private/get-order-detail", "code": 0,
            "result": {
                "order_id": "987654321", "client_oid": CLIENT_OID, "status": "FILLED",
                "cumulative_quantity": "0.0001", "cumulative_value": "6.25",
                "cumulative_fee": "0.01", "update_time": 1791579599000,
            },
        }])
        adapter = CryptoComSpotOrderAdapter(transport=transport, clock=lambda: NOW)
        result = adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET,
                                          order_id="987654321")
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.executed_quantity, Decimal("0.0001"))
        self.assertEqual(result.executed_value, Decimal("6.25"))
        self.assertEqual(result.fees, Decimal("0.01"))
        self.assertEqual(transport.calls[0][3], {"order_id": "987654321"})

    def test_uncertain_submission_can_be_reconciled_by_client_oid_without_retry(self):
        transport = FakeTransport([{
            "id": 3, "method": "private/get-order-detail", "code": 0,
            "result": {"order_id": "987654321", "client_oid": CLIENT_OID, "status": "CANCELED",
                       "cumulative_quantity": "0", "cumulative_value": "0", "cumulative_fee": "0"},
        }])
        adapter = CryptoComSpotOrderAdapter(transport=transport, clock=lambda: NOW)
        result = adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET,
                                          client_oid=CLIENT_OID)
        self.assertEqual(result.status, "CANCELED")
        self.assertEqual(transport.calls[0][3], {"client_oid": CLIENT_OID})

    def test_order_detail_requires_exactly_one_lookup_identity(self):
        adapter = CryptoComSpotOrderAdapter(transport=FakeTransport(), clock=lambda: NOW)
        for values in ({}, {"order_id": "1", "client_oid": CLIENT_OID}):
            with self.subTest(values=values), self.assertRaises(CryptoComSpotOrderError):
                adapter.get_order_detail(api_key=API_KEY, api_secret=API_SECRET, **values)

    def test_private_transport_signs_sorted_params_and_pins_method(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def read(self, _amount):
                return json.dumps({
                    "id": 101, "method": "private/create-order", "code": 0,
                    "result": {"order_id": "1", "client_oid": CLIENT_OID},
                }).encode()

        class Opener:
            def open(self, request, timeout):
                captured["url"] = request.full_url
                captured["body"] = json.loads(request.data)
                captured["timeout"] = timeout
                return Response()

        transport = CryptoComExchangePrivateTransport(nonce=lambda: 200, request_id=lambda: 101)
        with patch("invest_ai.execution.cryptocom_spot.urllib.request.build_opener", return_value=Opener()):
            transport.request("private/create-order", API_KEY, API_SECRET,
                              {"side": "BUY", "instrument_name": "BTC_USDT"})
        body = captured["body"]
        signed = "private/create-order" + "101" + API_KEY + "instrument_nameBTC_USDTsideBUY" + "200"
        expected = hmac.new(API_SECRET.encode(), signed.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(body["sig"], expected)
        self.assertEqual(body["params"], {"side": "BUY", "instrument_name": "BTC_USDT"})
        self.assertEqual(captured["url"], "https://api.crypto.com/exchange/v1/private/create-order")
        self.assertEqual(captured["timeout"], 8)

    def test_unexpected_response_method_or_client_oid_is_not_reported_as_submitted(self):
        for response in (
            {"id": 1, "method": "other", "code": 0,
             "result": {"order_id": "1", "client_oid": CLIENT_OID}},
            {"id": 1, "method": "private/create-order", "code": 0,
             "result": {"order_id": "1", "client_oid": "different"}},
        ):
            transport = FakeTransport([response])
            adapter = CryptoComSpotOrderAdapter(authorization_gate=FakeGate(),
                                                 instrument_verifier=FakeSpotVerifier(), transport=transport,
                                                 clock=lambda: NOW)
            with self.subTest(response=response), self.assertRaises(CryptoComSpotOrderError):
                adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)

    def test_missing_or_failed_spot_metadata_check_refuses_before_authorization(self):
        for verifier in (None, FakeSpotVerifier(reject=True)):
            gate, transport = FakeGate(), FakeTransport()
            adapter = CryptoComSpotOrderAdapter(authorization_gate=gate,
                                                 instrument_verifier=verifier, transport=transport,
                                                 clock=lambda: NOW)
            with self.subTest(verifier=verifier), self.assertRaises(CryptoComSpotOrderError):
                adapter.create_limit_order(intent(), api_key=API_KEY, api_secret=API_SECRET)
            self.assertEqual(gate.calls, [])
            self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
