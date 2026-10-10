from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from invest_ai.portfolio.ibkr_readonly import (
    IBKRLocalGatewayTransport,
    IBKRPortfolioError,
    IBKRReadOnlyPortfolioClient,
)


NOW = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
NOW_SECONDS = int(NOW.timestamp())


class FixtureTransport:
    def __init__(self, *, accounts=None, summary=None, positions=None):
        self.requests: list[str] = []
        self.payloads = {
            "/portfolio/accounts": accounts if accounts is not None else [
                {"accountId": "DU123456", "displayName": "Paper Account", "currency": "USD"},
                {"accountId": "U123456", "displayName": "Live Account", "currency": "USD"},
            ],
            "/portfolio/DU123456/summary": summary if summary is not None else {
                "netliquidation": {"amount": Decimal("1250.50"), "currency": "USD", "timestamp": NOW_MS},
                "totalcashvalue": {"amount": Decimal("500"), "currency": "USD", "timestamp": NOW_MS},
                "accountcode": {"amount": 0, "currency": None, "timestamp": NOW_MS, "value": "DU123456"},
            },
            "/portfolio2/DU123456/positions": positions if positions is not None else [{
                "acctId": "DU123456", "conid": 265598, "description": "AAPL",
                "assetClass": "STK", "position": 2, "currency": "USD",
                "avgPrice": Decimal("180.25"), "marketPrice": Decimal("190.5"),
                "marketValue": Decimal("381"),
                "timestamp": NOW_SECONDS,
            }],
        }

    def get_json(self, path):
        self.requests.append(path)
        if path not in self.payloads:
            raise AssertionError(f"Unexpected Gateway route: {path}")
        payload = self.payloads[path]
        body = json.dumps(
            payload, separators=(",", ":"), allow_nan=False,
            default=lambda value: (int(value) if value == value.to_integral_value() else float(value))
            if isinstance(value, Decimal) else None,
        ).encode()
        return payload, body


def client(transport: FixtureTransport) -> IBKRReadOnlyPortfolioClient:
    return IBKRReadOnlyPortfolioClient(transport, clock=lambda: NOW)


class IBKRReadOnlyPortfolioTests(unittest.TestCase):
    def test_account_discovery_is_separate_and_does_not_choose_an_account(self) -> None:
        transport = FixtureTransport()
        accounts = client(transport).list_accounts()
        self.assertEqual([item.account_id for item in accounts], ["DU123456", "U123456"])
        self.assertEqual(transport.requests, ["/portfolio/accounts"])

    def test_import_requires_explicit_account_and_returns_observation_only_snapshot(self) -> None:
        transport = FixtureTransport()
        snapshot = client(transport).read_portfolio("DU123456")
        self.assertEqual(transport.requests, [
            "/portfolio/accounts", "/portfolio/DU123456/summary",
            "/portfolio2/DU123456/positions",
        ])
        self.assertEqual(snapshot.account.account_id, "DU123456")
        self.assertEqual(snapshot.metrics[0].amount, Decimal("1250.5"))
        self.assertEqual(snapshot.metrics[0].currency, "USD")
        self.assertEqual(snapshot.positions[0].quantity, Decimal("2"))
        self.assertEqual(snapshot.positions[0].market_value, Decimal("381"))
        self.assertEqual(snapshot.positions[0].provider_timestamp, NOW)
        self.assertEqual(len(snapshot.accounts_payload_sha256), 64)
        self.assertEqual(snapshot.authority_status, "BROKER_READ_ONLY_OBSERVATION_ONLY")
        self.assertFalse(snapshot.persistent)
        self.assertFalse(snapshot.usable_for_risk)
        self.assertFalse(snapshot.usable_for_execution)

    def test_account_not_returned_by_gateway_is_rejected_before_other_reads(self) -> None:
        transport = FixtureTransport()
        with self.assertRaises(IBKRPortfolioError):
            client(transport).read_portfolio("OTHER")
        self.assertEqual(transport.requests, ["/portfolio/accounts"])

    def test_position_from_another_account_is_rejected(self) -> None:
        transport = FixtureTransport(positions=[{
            "acctId": "U123456", "conid": 1, "description": "AAPL", "assetClass": "STK",
            "position": 1, "currency": "USD", "timestamp": NOW_SECONDS,
        }])
        with self.assertRaisesRegex(IBKRPortfolioError, "does not match"):
            client(transport).read_portfolio("DU123456")

    def test_stale_or_future_position_is_rejected(self) -> None:
        for timestamp in (NOW_SECONDS - 120, NOW_SECONDS + 1):
            with self.subTest(timestamp=timestamp):
                transport = FixtureTransport(positions=[{
                    "acctId": "DU123456", "conid": 1, "description": "AAPL", "assetClass": "STK",
                    "position": 1, "currency": "USD", "timestamp": timestamp,
                }])
                with self.assertRaisesRegex(IBKRPortfolioError, "future-dated or stale"):
                    client(transport).read_portfolio("DU123456")

    def test_duplicate_contracts_are_rejected(self) -> None:
        position = {
            "acctId": "DU123456", "conid": 1, "description": "AAPL", "assetClass": "STK",
            "position": 1, "currency": "USD", "timestamp": NOW_SECONDS,
        }
        transport = FixtureTransport(positions=[position, position])
        with self.assertRaisesRegex(IBKRPortfolioError, "unique positive"):
            client(transport).read_portfolio("DU123456")

    def test_position_without_currency_or_timestamp_is_rejected(self) -> None:
        for omitted in ("currency", "timestamp"):
            with self.subTest(omitted=omitted):
                position = {
                    "acctId": "DU123456", "conid": 1, "description": "AAPL", "assetClass": "STK",
                    "position": 1, "currency": "USD", "timestamp": NOW_SECONDS,
                }
                position.pop(omitted)
                transport = FixtureTransport(positions=[position])
                with self.assertRaises(IBKRPortfolioError):
                    client(transport).read_portfolio("DU123456")

    def test_float_amount_is_rejected_to_avoid_binary_money_conversion(self) -> None:
        transport = FixtureTransport(summary={
            "netliquidation": {"amount": 12.34, "currency": "USD", "timestamp": NOW_MS},
        })
        with self.assertRaisesRegex(IBKRPortfolioError, "Invalid decimal"):
            client(transport).read_portfolio("DU123456")

    def test_duplicate_account_ids_are_rejected(self) -> None:
        transport = FixtureTransport(accounts=[
            {"accountId": "DU123456", "currency": "USD"},
            {"id": "DU123456", "currency": "USD"},
        ])
        with self.assertRaisesRegex(IBKRPortfolioError, "unique"):
            client(transport).list_accounts()

    def test_metric_and_position_time_skew_is_rejected(self) -> None:
        transport = FixtureTransport(positions=[{
            "acctId": "DU123456", "conid": 1, "description": "AAPL", "assetClass": "STK",
            "position": 1, "currency": "USD", "timestamp": NOW_SECONDS - 120,
        }])
        importer = IBKRReadOnlyPortfolioClient(
            transport, clock=lambda: NOW, max_snapshot_age=timedelta(minutes=5),
            max_snapshot_skew=timedelta(seconds=30),
        )
        with self.assertRaisesRegex(IBKRPortfolioError, "time skew"):
            importer.read_portfolio("DU123456")

    def test_gateway_transport_rejects_non_allowlisted_path_before_authentication(self) -> None:
        class NoNetworkSession:
            def tickle(self):
                raise AssertionError("Invalid path must be rejected before any Gateway access.")

        with tempfile.TemporaryDirectory() as directory:
            ca_file = Path(directory) / "test-ca.pem"
            ca_file.write_text("fixture only", encoding="utf-8")
            transport = IBKRLocalGatewayTransport(ca_file, session=NoNetworkSession())  # type: ignore[arg-type]
            for path in ("/iserver/account/orders", "/portfolio/../accounts", "/portfolio/accounts?x=1"):
                with self.subTest(path=path), self.assertRaises(IBKRPortfolioError):
                    transport.get_json(path)


if __name__ == "__main__":
    unittest.main()
