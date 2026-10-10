from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from invest_ai.portfolio.cryptocom_app_readonly import (
    CryptoComAppPortfolioError,
    CryptoComAppReadOnlyClient,
    CryptoComAppTransport,
)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def get_json(self, path: str, api_key: str, api_secret: str):
        self.calls.append((path, api_key, api_secret))
        if path == "/v1/fiat-account":
            payload = {"ok": True, "account": {"balances": [
                {"currency": "EUR", "amount": {"amount": "12.3400"}},
                {"currency": "USD", "amount": {"amount": "0"}},
            ]}}
        elif path == "/v1/crypto-account":
            payload = {"ok": True, "account": {"wallets": [
                {"currency": "BTC", "available": {"amount": "0.001"},
                 "balance": {"amount": "0.0012"}},
            ]}}
        else:
            raise AssertionError(f"Unexpected API path: {path}")
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return payload, raw


class CryptoComAppReadOnlyTests(unittest.TestCase):
    def test_reads_only_fiat_and_crypto_balances_and_marks_observation_non_authoritative(self) -> None:
        transport = FakeTransport()
        observed_at = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        snapshot = CryptoComAppReadOnlyClient(transport, clock=lambda: observed_at).read_balances(
            "agent-key", "agent-secret"
        )

        self.assertEqual([call[0] for call in transport.calls], ["/v1/fiat-account", "/v1/crypto-account"])
        self.assertEqual(snapshot.fetched_at, observed_at)
        self.assertEqual([(item.currency, item.account_type, item.total, item.available) for item in snapshot.balances], [
            ("EUR", "FIAT", Decimal("12.3400"), Decimal("12.3400")),
            ("BTC", "CRYPTO", Decimal("0.0012"), Decimal("0.001")),
        ])
        self.assertEqual(set(snapshot.payload_sha256), {"fiat", "crypto"})
        self.assertTrue(all(len(item.payload_sha256) == 64 for item in snapshot.balances))
        self.assertFalse(snapshot.persistent)
        self.assertFalse(snapshot.usable_for_risk)
        self.assertFalse(snapshot.usable_for_execution)

    def test_rejects_invalid_or_empty_credentials_before_network(self) -> None:
        transport = FakeTransport()
        client = CryptoComAppReadOnlyClient(transport)
        for key, secret in ((" ", "secret"), ("key", ""), ("k" * 513, "secret")):
            with self.subTest(key_length=len(key), secret=bool(secret)), self.assertRaises(CryptoComAppPortfolioError):
                client.read_balances(key, secret)
        self.assertEqual(transport.calls, [])

    def test_transport_signs_exact_allowlisted_get_and_refuses_other_route(self) -> None:
        import invest_ai.portfolio.cryptocom_app_readonly as module

        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return b'{"ok":true}'

        class Opener:
            def open(self, request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return Response()

        original_build_opener = module.urllib.request.build_opener
        original_time_ns = module.time.time_ns
        try:
            module.urllib.request.build_opener = lambda *_handlers: Opener()
            module.time.time_ns = lambda: 1_700_000_000_123_000_000
            _payload, raw = CryptoComAppTransport().get_json("/v1/fiat-account", "key", "secret")
        finally:
            module.urllib.request.build_opener = original_build_opener
            module.time.time_ns = original_time_ns

        request = captured["request"]
        timestamp = "1700000000123"
        message = timestamp + "GET/v1/fiat-account"
        expected_signature = base64.b64encode(
            hmac.new(b"secret", message.encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(request.full_url, "https://wapi.crypto.com/v1/fiat-account")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Cdc-api-key"), "key")
        self.assertEqual(request.get_header("Cdc-api-timestamp"), timestamp)
        self.assertEqual(request.get_header("Cdc-api-signature"), expected_signature)
        self.assertEqual(captured["timeout"], 8)
        self.assertEqual(raw, b'{"ok":true}')
        with self.assertRaises(CryptoComAppPortfolioError):
            CryptoComAppTransport().get_json("/v1/private/create-order", "key", "secret")

    def test_rejects_bad_provider_payload_without_echoing_credentials(self) -> None:
        class RejectedTransport:
            def get_json(self, path, _api_key, _api_secret):
                return {"ok": False}, b'{"ok":false}'

        key, secret = "private-key", "private-secret"
        with self.assertRaises(CryptoComAppPortfolioError) as raised:
            CryptoComAppReadOnlyClient(RejectedTransport()).read_balances(key, secret)
        self.assertNotIn(key, str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
