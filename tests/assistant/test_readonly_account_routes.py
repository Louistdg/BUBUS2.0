from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from invest_ai.assistant.market_dashboard import make_server
from invest_ai.portfolio.cryptocom_readonly import CryptoComPortfolioError, CryptoComReadOnlyPortfolioClient


class CryptoFixtureTransport:
    def __init__(self):
        self.methods = []

    def post_balance(self, body):
        request = json.loads(body)
        self.methods.append(request["method"])
        payload = {"code": 0, "method": request["method"], "result": {"data": [
            {"instrument_name": "BTC", "total_available_balance": "0.001",
             "total_margin_balance": "0.001", "total_initial_margin": "0", "total_cash_balance": "0.001",
             "total_isolated_cash_balance": "0", "is_liquidating": False,
             "position_balances": [{"instrument_name": "BTC", "quantity": "0.001",
                 "market_value": "60", "collateral_amount": "57", "max_withdrawal_balance": "0.001",
                 "reserved_qty": "0"}]},
        ]}}
        return payload, json.dumps(payload).encode()

    def post_positions(self, body):
        request = json.loads(body)
        self.methods.append(request["method"])
        payload = {"code": 0, "method": request["method"], "result": {"data": []}}
        return payload, json.dumps(payload).encode()

    def post_open_orders(self, body):
        request = json.loads(body)
        self.methods.append(request["method"])
        payload = {"code": 0, "method": request["method"], "result": {"data": [{
            "account_id": "fixture-account", "order_id": 42, "client_oid": "fixture-open-order",
            "instrument_name": "BTC_USDT", "side": "BUY", "order_type": "LIMIT", "status": "ACTIVE",
            "quantity": "0.01", "cumulative_quantity": "0.002", "limit_price": "60000",
            "create_time": 1791560000000, "update_time": 1791560100000,
        }]}}
        return payload, json.dumps(payload).encode()


class FakeIBKRPortfolioClient:
    def __init__(self):
        self.calls = []

    def list_accounts(self):
        self.calls.append("list_accounts")
        return [SimpleNamespace(account_id="DU123456", display_name="Compte de test", currency="EUR")]

    def read_portfolio(self, account_id):
        self.calls.append(("read_portfolio", account_id))
        if account_id != "DU123456":
            raise AssertionError("Unlisted account must not be read by the route test.")
        now = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)
        return SimpleNamespace(
            account=SimpleNamespace(account_id=account_id, display_name="Compte de test", currency="EUR"),
            fetched_at=now, authority_status="BROKER_READ_ONLY_OBSERVATION_ONLY", persistent=False,
            usable_for_risk=False, usable_for_execution=False,
            metrics=[SimpleNamespace(name="netliquidation", amount=Decimal("1250"), currency="EUR",
                                     provider_timestamp=now, source="fixture", payload_sha256="a" * 64)],
            positions=[],
        )


class MemoryCredentialStore:
    available = True

    def __init__(self):
        self.credentials = None

    def has_credentials(self):
        return self.credentials is not None

    def store(self, api_key, api_secret):
        self.credentials = (api_key, api_secret)

    def load(self):
        if self.credentials is None:
            return None
        return SimpleNamespace(api_key=self.credentials[0], api_secret=self.credentials[1])

    def delete(self):
        self.credentials = None


class FailingCryptoPortfolioClient:
    def read_portfolio(self, _api_key, _api_secret):
        raise CryptoComPortfolioError("Invalid credentials.")


class ReadOnlyAccountRouteTests(unittest.TestCase):
    def setUp(self):
        self.crypto_transport = CryptoFixtureTransport()
        self.credential_store = MemoryCredentialStore()
        self.crypto_client = CryptoComReadOnlyPortfolioClient(
            self.crypto_transport, clock=lambda: datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc),
            nonce=lambda: 123456,
        )
        self.ibkr_client = FakeIBKRPortfolioClient()
        self.server = make_server(0, portfolio_client=self.ibkr_client,
                                  cryptocom_portfolio_client=self.crypto_client,
                                  cryptocom_credential_store=self.credential_store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _post(self, path, payload, *, origin):
        request = Request(self.base + path, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json", "Origin": origin}, method="POST")
        return urlopen(request, timeout=2)

    def test_crypto_import_is_explicit_and_never_echoes_credentials(self):
        api_key, api_secret = "temporary-api-key", "temporary-api-secret"
        with self.assertRaises(HTTPError) as raised:
            self._post("/api/cryptocom/portfolio/import", {"api_key": api_key, "api_secret": api_secret},
                       origin="http://evil.example")
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        self.assertEqual(self.crypto_transport.methods, [])
        with self._post("/api/cryptocom/portfolio/import", {"api_key": api_key, "api_secret": api_secret},
                        origin=self.base) as response:
            body = response.read()
        result = json.loads(body)
        self.assertNotIn(api_key.encode(), body)
        self.assertNotIn(api_secret.encode(), body)
        self.assertEqual(self.crypto_transport.methods,
                         ["private/user-balance", "private/get-positions", "private/get-open-orders"])
        self.assertEqual(result["balances"][0]["currency"], "BTC")
        self.assertEqual(result["wallet_balances"][0]["currency"], "BTC")
        self.assertEqual(result["wallet_balances"][0]["quantity"], "0.001")
        self.assertEqual(result["positions"], [])
        self.assertEqual(len(result["open_orders"]), 1)
        self.assertEqual(result["open_orders"][0]["order_id"], "42")
        self.assertEqual(result["open_orders"][0]["cumulative_quantity"], "0.002")
        self.assertEqual(result["open_orders"][0]["source"], "private/get-open-orders")
        self.assertFalse(result["persistent"])
        self.assertFalse(result["usable_for_execution"])
        self.assertFalse(result["credentials_persisted"])
        self.assertIsNone(self.credential_store.credentials)

    def test_ibkr_account_discovery_and_import_happen_only_after_same_origin_actions(self):
        with urlopen(self.base + "/api/ibkr/portfolio/config", timeout=2) as response:
            self.assertTrue(json.loads(response.read())["configured"])
        self.assertEqual(self.ibkr_client.calls, [])
        request = Request(self.base + "/api/ibkr/portfolio/accounts", headers={"Origin": "http://evil.example"})
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        self.assertEqual(self.ibkr_client.calls, [])

        with urlopen(Request(self.base + "/api/ibkr/portfolio/accounts", headers={"Origin": self.base}), timeout=2) as response:
            accounts = json.loads(response.read())["accounts"]
        self.assertEqual(accounts[0]["account_id"], "DU123456")
        request = Request(self.base + "/api/ibkr/portfolio/import", data=b'{"account_id":"OTHER"}',
                          headers={"Content-Type": "application/json", "Origin": self.base}, method="POST")
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 503)
        raised.exception.close()
        self.assertEqual(self.ibkr_client.calls, ["list_accounts", ("read_portfolio", "OTHER")])

        with self._post("/api/ibkr/portfolio/import", {"account_id": "DU123456"}, origin=self.base) as response:
            data = json.loads(response.read())
        self.assertEqual(data["account"]["currency"], "EUR")
        self.assertEqual(data["metrics"][0]["amount"], "1250")
        self.assertFalse(data["usable_for_risk"])
        self.assertFalse(data["usable_for_execution"])

    def test_crypto_credentials_are_saved_only_after_success_and_only_on_explicit_choice(self):
        with urlopen(Request(self.base + "/api/cryptocom/portfolio/config",
                             headers={"Origin": self.base}), timeout=2) as response:
            initial = json.loads(response.read())
        self.assertTrue(initial["available"])
        self.assertFalse(initial["credentials_persisted"])
        self.assertEqual(self.crypto_transport.methods, [])

        api_key, api_secret = "valid-api-key", "matching-api-secret"
        with self._post("/api/cryptocom/portfolio/import", {
            "api_key": api_key, "api_secret": api_secret, "remember_credentials": True,
        }, origin=self.base) as response:
            imported = json.loads(response.read())
        self.assertTrue(imported["credentials_persisted"])
        self.assertEqual(self.credential_store.credentials, (api_key, api_secret))
        self.assertNotIn(api_key, json.dumps(imported))
        self.assertNotIn(api_secret, json.dumps(imported))

        with self._post("/api/cryptocom/portfolio/refresh", {"use_saved_credentials": True},
                        origin=self.base) as response:
            refreshed = json.loads(response.read())
        self.assertEqual(refreshed["balances"][0]["currency"], "BTC")
        self.assertTrue(refreshed["credentials_persisted"])
        self.assertEqual(self.crypto_transport.methods,
                         ["private/user-balance", "private/get-positions", "private/get-open-orders"] * 2)

        with self._post("/api/cryptocom/portfolio/forget", {"forget_saved_credentials": True},
                        origin=self.base) as response:
            forgotten = json.loads(response.read())
        self.assertTrue(forgotten["forgotten"])
        self.assertFalse(forgotten["credentials_persisted"])
        self.assertIsNone(self.credential_store.credentials)
        self.assertEqual(len(self.crypto_transport.methods), 6)

    def test_crypto_refresh_can_use_the_separate_observer_read_key(self):
        observer_store = MemoryCredentialStore()
        observer_store.store("observer-key", "observer-secret")
        self.server.cryptocom_observer_credential_store = observer_store
        with self._post("/api/cryptocom/portfolio/refresh", {"use_observer_credentials": True},
                        origin=self.base) as response:
            refreshed = json.loads(response.read())
        self.assertEqual(refreshed["balances"][0]["currency"], "BTC")
        self.assertTrue(refreshed["credentials_persisted"])
        self.assertEqual(self.crypto_transport.methods,
                         ["private/user-balance", "private/get-positions", "private/get-open-orders"])
        self.assertIsNone(self.credential_store.credentials)

    def test_crypto_saved_refresh_and_forget_reject_cross_origin(self):
        self.credential_store.store("valid-api-key", "matching-api-secret")
        for path, payload in (
            ("/api/cryptocom/portfolio/refresh", {"use_saved_credentials": True}),
            ("/api/cryptocom/portfolio/forget", {"forget_saved_credentials": True}),
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                self._post(path, payload, origin="http://evil.example")
            self.assertEqual(raised.exception.code, 403)
            raised.exception.close()
        self.assertEqual(self.crypto_transport.methods, [])
        self.assertIsNotNone(self.credential_store.credentials)

    def test_crypto_credentials_are_not_saved_when_read_fails(self):
        server = make_server(0, cryptocom_portfolio_client=FailingCryptoPortfolioClient(),
                             cryptocom_credential_store=self.credential_store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(base + "/api/cryptocom/portfolio/import",
                              data=json.dumps({"api_key": "bad", "api_secret": "bad-secret",
                                               "remember_credentials": True}).encode(),
                              headers={"Content-Type": "application/json", "Origin": base}, method="POST")
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 503)
            raised.exception.close()
            self.assertIsNone(self.credential_store.credentials)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
