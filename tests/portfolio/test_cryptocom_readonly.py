from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from urllib.error import HTTPError
from datetime import datetime, timezone
from decimal import Decimal

from invest_ai.portfolio.cryptocom_readonly import (
    CryptoComTransaction,
    CryptoComExecutedTrade,
    CryptoComPortfolioError,
    CryptoComReadOnlyPortfolioClient,
)


NOW = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)


class FixtureTransport:
    def __init__(self, payload=None, positions_payload=None, open_orders_payload=None):
        self.body = None
        self.positions_body = None
        self.open_orders_body = None
        self.payload = payload if payload is not None else {
            "code": 0,
            "method": "private/user-balance",
            "result": {"data": [
                {"instrument_name": "EUR", "total_available_balance": "12.00",
                 "total_margin_balance": "12.00", "total_initial_margin": "0", "total_cash_balance": "12.00",
                 "total_isolated_cash_balance": "0", "is_liquidating": False},
                {"instrument_name": "BTC", "total_available_balance": "0.001",
                 "total_margin_balance": "0.001", "total_initial_margin": "0", "total_cash_balance": "0.001",
                 "total_isolated_cash_balance": "0", "is_liquidating": False},
            ]},
        }
        self.positions_payload = positions_payload if positions_payload is not None else {
            "code": 0, "method": "private/get-positions", "result": {"data": []},
        }
        self.open_orders_payload = open_orders_payload if open_orders_payload is not None else {
            "code": 0, "method": "private/get-open-orders", "result": {"data": []},
        }
        self.transactions_body = None
        self.transactions_payload = {
            "code": 0, "method": "private/get-transactions", "result": {"data": []},
        }
        self.trades_body = None
        self.trades_payload = {"code": 0, "method": "private/get-trades", "result": {"data": []}}
        self.fee_body = None
        self.fee_payload = {"code": 0, "method": "private/get-fee-rate", "result": {
            "spot_tier": "5", "effective_spot_maker_rate_bps": "25", "effective_spot_taker_rate_bps": "50",
        }}
        self.balance_history_body = None
        self.balance_history_payload = {"code": 0, "method": "private/user-balance-history", "result": {
            "instrument_name": "EUR", "data": [{"t": 1791561600000, "c": "12.00"},
                                               {"t": 1791565200000, "c": "13.25"}]}}
        self.deposit_history_body = None
        self.deposit_history_payload = {"code": 0, "method": "private/get-deposit-history", "result": {
            "deposit_list": []}}
        self.withdrawal_history_body = None
        self.withdrawal_history_payload = {"code": 0, "method": "private/get-withdrawal-history", "result": {
            "withdrawal_list": []}}

    def post_balance(self, body):
        self.body = body
        return self.payload, json.dumps(self.payload, separators=(",", ":")).encode()

    def post_positions(self, body):
        self.positions_body = body
        return self.positions_payload, json.dumps(self.positions_payload, separators=(",", ":")).encode()

    def post_open_orders(self, body):
        self.open_orders_body = body
        return self.open_orders_payload, json.dumps(self.open_orders_payload, separators=(",", ":")).encode()

    def post_transactions(self, body):
        self.transactions_body = body
        return self.transactions_payload, json.dumps(self.transactions_payload, separators=(",", ":")).encode()

    def post_trades(self, body):
        self.trades_body = body
        return self.trades_payload, json.dumps(self.trades_payload, separators=(",", ":")).encode()

    def post_fee_rate(self, body):
        self.fee_body = body
        return self.fee_payload, json.dumps(self.fee_payload, separators=(",", ":")).encode()

    def post_balance_history(self, body):
        self.balance_history_body = body
        return self.balance_history_payload, json.dumps(self.balance_history_payload, separators=(",", ":")).encode()

    def post_deposit_history(self, body):
        self.deposit_history_body = body
        return self.deposit_history_payload, json.dumps(self.deposit_history_payload, separators=(",", ":")).encode()

    def post_withdrawal_history(self, body):
        self.withdrawal_history_body = body
        return self.withdrawal_history_payload, json.dumps(self.withdrawal_history_payload, separators=(",", ":")).encode()


class CryptoComReadOnlyTests(unittest.TestCase):
    def test_trade_pagination_splits_capped_windows_and_preserves_uncovered_ranges(self):
        client = CryptoComReadOnlyPortfolioClient(FixtureTransport(), clock=lambda: NOW, nonce=lambda: 123456)

        def trade(identity, timestamp_ns):
            return CryptoComExecutedTrade(str(identity), "10", str(identity), None, "BTC_USDT", "BUY", None,
                Decimal("1"), Decimal("2"), Decimal("0"), "USDT",
                datetime.fromtimestamp(timestamp_ns / 1_000_000_000, timezone.utc), str(timestamp_ns),
                "private/get-trades", "a" * 64)

        def capped_parent(api_key, api_secret, *, start_time_ns, end_time_ns, limit):
            if (start_time_ns, end_time_ns) == (0, 100):
                return tuple(trade(i + 1, 1) for i in range(100)), "b" * 64
            return (trade(start_time_ns + 1, start_time_ns + 1),), "d" * 64

        client.read_trades = capped_parent
        result = client.read_trades_partitioned("key", "secret", start_time_ns=0, end_time_ns=100, max_requests=8)
        self.assertTrue(result.complete)
        self.assertEqual(result.request_count, 3)
        self.assertEqual([(page.start_time_ns, page.end_time_ns) for page in result.pages], [(0, 50), (50, 100)])

        def saturated(api_key, api_secret, *, start_time_ns, end_time_ns, limit):
            return tuple(trade(i + 1, start_time_ns) for i in range(limit)), "c" * 64

        client.read_trades = saturated
        stopped = client.read_trades_partitioned("key", "secret", start_time_ns=0, end_time_ns=100, max_requests=1)
        self.assertFalse(stopped.complete)
        self.assertEqual(stopped.request_count, 1)
        self.assertEqual(stopped.incomplete_intervals, ((0, 50), (50, 100)))

    def test_transaction_pagination_splits_capped_windows_and_reports_uncovered_ranges(self):
        client = CryptoComReadOnlyPortfolioClient(FixtureTransport(), clock=lambda: NOW, nonce=lambda: 123456)

        def transaction(journal_id, timestamp_ms):
            return CryptoComTransaction("TRADING", str(journal_id), Decimal("1"), Decimal("1"),
                Decimal("0"), None, None, "BTC_USDT", "BUY",
                datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc),
                "private/get-transactions", "a" * 64)

        def capped_parent(api_key, api_secret, *, start_time_ms, end_time_ms, limit):
            if (start_time_ms, end_time_ms) == (0, 100):
                return tuple(transaction(f"parent-{i}", 1) for i in range(100)), "b" * 64
            return (transaction(f"{start_time_ms}-{end_time_ms}", start_time_ms),), "d" * 64

        client.read_transactions = capped_parent
        result = client.read_transactions_partitioned(
            "key", "secret", start_time_ms=0, end_time_ms=100, max_requests=8,
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.request_count, 3)
        self.assertEqual([(page.start_time_ms, page.end_time_ms) for page in result.pages], [(0, 50), (50, 100)])
        self.assertEqual([page.items[0].journal_id for page in result.pages], ["0-50", "50-100"])

        def capped_all(api_key, api_secret, *, start_time_ms, end_time_ms, limit):
            return tuple(transaction(f"same-{i}", start_time_ms) for i in range(limit)), "c" * 64

        client.read_transactions = capped_all
        saturated = client.read_transactions_partitioned(
            "key", "secret", start_time_ms=0, end_time_ms=1, max_requests=8,
        )
        self.assertFalse(saturated.complete)
        self.assertEqual(saturated.incomplete_intervals, ((0, 1),))
        stopped = client.read_transactions_partitioned(
            "key", "secret", start_time_ms=0, end_time_ms=100, max_requests=1,
        )
        self.assertFalse(stopped.complete)
        self.assertEqual(stopped.request_count, 1)
        self.assertEqual(stopped.incomplete_intervals, ((0, 50), (50, 100)))

    def test_executed_trade_history_is_signed_ns_bounded_and_read_only(self):
        start_ns = int(NOW.timestamp() * 1_000_000_000)
        end_ns = start_ns + 10_000_000_000
        trade_ns = start_ns + 1_234_000_000
        payload = {"code": 0, "method": "private/get-trades", "result": {"data": [{
            "trade_id": "987", "order_id": "654", "trade_match_id": "321", "client_oid": "local-order-1",
            "instrument_name": "BTC_USDT", "side": "BUY", "taker_side": "MAKER",
            "traded_quantity": "0.0125", "traded_price": "64000.5", "fees": "-0.200001",
            "fee_instrument_name": "USDT", "create_time": trade_ns // 1_000_000,
            "create_time_ns": str(trade_ns),
        }]}}
        transport = FixtureTransport()
        transport.trades_payload = payload
        client = CryptoComReadOnlyPortfolioClient(transport, clock=lambda: NOW, nonce=lambda: 123456)
        trades, digest = client.read_trades("fixture-key", "fixture-secret", start_time_ns=start_ns,
            end_time_ns=end_ns, limit=10)
        request = json.loads(transport.trades_body)
        params = request["params"]
        parameter_string = "".join(key + str(params[key]) for key in sorted(params))
        expected = hmac.new(b"fixture-secret",
            f"private/get-trades123456fixture-key{parameter_string}123456".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(request["sig"], expected)
        self.assertEqual(request["method"], "private/get-trades")
        self.assertEqual(params["start_time"], str(start_ns))
        self.assertEqual(trades[0].trade_id, "987")
        self.assertEqual(trades[0].quantity, Decimal("0.0125"))
        self.assertEqual(trades[0].price, Decimal("64000.5"))
        self.assertEqual(trades[0].fees, Decimal("-0.200001"))
        self.assertEqual(trades[0].fee_currency, "USDT")
        self.assertEqual(trades[0].timestamp_ns, str(trade_ns))
        self.assertEqual(len(digest), 64)

    def test_executed_trade_history_rejects_invalid_bounds_rows_and_duplicate_ids(self):
        start_ns = int(NOW.timestamp() * 1_000_000_000)
        end_ns = start_ns + 10_000_000_000
        client = CryptoComReadOnlyPortfolioClient(FixtureTransport(), clock=lambda: NOW, nonce=lambda: 123456)
        for kwargs in ({"start_time_ns": end_ns, "end_time_ns": end_ns},
                       {"start_time_ns": start_ns, "end_time_ns": end_ns, "limit": 101}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CryptoComPortfolioError):
                client.read_trades("key", "secret", **kwargs)
        row = {"trade_id": "12", "order_id": "13", "trade_match_id": "14",
            "instrument_name": "BTC_USDT", "side": "BUY", "traded_quantity": "1",
            "traded_price": "2", "fees": "-0.1", "fee_instrument_name": "USDT",
            "create_time": start_ns // 1_000_000 + 1,
            "create_time_ns": str(start_ns + 1_000_000)}
        transport = FixtureTransport()
        transport.trades_payload = {"code": 0, "method": "private/get-trades",
            "result": {"data": [row, row]}}
        with self.assertRaisesRegex(CryptoComPortfolioError, "duplicate trade identities"):
            CryptoComReadOnlyPortfolioClient(transport, clock=lambda: NOW, nonce=lambda: 123456).read_trades(
                "key", "secret", start_time_ns=start_ns, end_time_ns=end_ns)

    def test_balance_history_is_cash_only_bounded_and_correctly_signed(self):
        transport = FixtureTransport()
        client = CryptoComReadOnlyPortfolioClient(transport, clock=lambda: NOW, nonce=lambda: 123456)
        history = client.read_balance_history("fixture-key", "fixture-secret", timeframe="H1", limit=24)
        self.assertEqual(history.timeframe, "H1")
        self.assertEqual(len(history.points), 2)
        self.assertEqual(history.points[-1].total_cash, Decimal("13.25"))
        self.assertEqual(history.points[-1].currency, "EUR")
        request = json.loads(transport.balance_history_body)
        params = request["params"]
        self.assertEqual(params, {"timeframe": "H1", "limit": 24})
        parameter_string = "".join(key + str(params[key]) for key in sorted(params))
        expected = hmac.new(b"fixture-secret",
            f"private/user-balance-history123456fixture-key{parameter_string}123456".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(request["sig"], expected)

    def test_balance_history_refuses_invalid_page_and_ambiguous_rows(self):
        client = CryptoComReadOnlyPortfolioClient(FixtureTransport(), clock=lambda: NOW, nonce=lambda: 123456)
        for timeframe, limit in (("M1", 24), ("H1", 121), ("D1", 31), ("H1", True)):
            with self.subTest(timeframe=timeframe, limit=limit), self.assertRaises(CryptoComPortfolioError):
                client.read_balance_history("key", "secret", timeframe=timeframe, limit=limit)
        transport = FixtureTransport()
        transport.balance_history_payload["result"]["data"].append({"t": 1791561600000, "c": "12.5"})
        with self.assertRaisesRegex(CryptoComPortfolioError, "duplicated"):
            CryptoComReadOnlyPortfolioClient(transport, clock=lambda: NOW, nonce=lambda: 123456).read_balance_history(
                "key", "secret")

    def test_funding_history_reads_currency_amount_status_and_signs_read_only_request(self):
        start_ms = int((NOW.timestamp() - 3600) * 1000)
        end_ms = int((NOW.timestamp() + 3600) * 1000)
        created = start_ms + 1000
        transport = FixtureTransport()
        transport.deposit_history_payload = {"code": 0, "method": "private/get-deposit-history", "result": {
            "deposit_list": [{"id": "dep-1", "currency": "BTC", "amount": "0.25", "fee": "0.001",
                "create_time": created, "update_time": created + 10, "status": "1"}]}}
        client = CryptoComReadOnlyPortfolioClient(transport, clock=lambda: NOW, nonce=lambda: 123456)
        history = client.read_funding_history("fixture-key", "fixture-secret", direction="DEPOSIT",
            start_time_ms=start_ms, end_time_ms=end_ms, page_size=20)
        self.assertTrue(history.complete)
        self.assertEqual(history.request_count, 1)
        item = history.pages[0].items[0]
        self.assertEqual((item.direction, item.transfer_id, item.currency, item.status),
                         ("DEPOSIT", "dep-1", "BTC", "1"))
        self.assertEqual((item.amount, item.fee), (Decimal("0.25"), Decimal("0.001")))
        request = json.loads(transport.deposit_history_body)
        self.assertEqual(request["method"], "private/get-deposit-history")
        self.assertEqual(request["params"], {"start_ts": start_ms, "end_ts": end_ms,
            "page_size": 20, "page": 0})
        parameter_string = "".join(key + str(request["params"][key]) for key in sorted(request["params"]))
        expected = hmac.new(b"fixture-secret",
            f"private/get-deposit-history123456fixture-key{parameter_string}123456".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(request["sig"], expected)

        transport.withdrawal_history_payload = {"code": 0, "method": "private/get-withdrawal-history", "result": {
            "withdrawal_list": [{"id": "with-2", "currency": "USDT", "amount": "12.5", "fee": "0.5",
                "create_time": created, "update_time": created + 10, "status": 5}]}}
        withdrawal = client.read_funding_history("fixture-key", "fixture-secret", direction="WITHDRAWAL",
            start_time_ms=start_ms, end_time_ms=end_ms, page_size=20)
        self.assertEqual(withdrawal.pages[0].items[0].currency, "USDT")
        self.assertEqual(withdrawal.pages[0].items[0].status, "5")
        self.assertEqual(json.loads(transport.withdrawal_history_body)["method"], "private/get-withdrawal-history")

    def test_funding_history_paginates_and_marks_a_full_page_cap_incomplete(self):
        start_ms = int((NOW.timestamp() - 3600) * 1000)
        end_ms = int((NOW.timestamp() + 3600) * 1000)
        transport = FixtureTransport()

        def deposits(body):
            request = json.loads(body)
            page_number = request["params"]["page"]
            rows = [{"id": f"dep-{page_number}-{index}", "currency": "BTC", "amount": "1", "fee": "0",
                "create_time": start_ms + 1000 + page_number * 10 + index,
                "update_time": start_ms + 1000 + page_number * 10 + index, "status": "1"}
                for index in range(2 if page_number == 0 else 1)]
            payload = {"code": 0, "method": "private/get-deposit-history", "result": {"deposit_list": rows}}
            return payload, json.dumps(payload, separators=(",", ":")).encode()

        transport.post_deposit_history = deposits
        client = CryptoComReadOnlyPortfolioClient(transport, clock=lambda: NOW, nonce=lambda: 123456)
        complete = client.read_funding_history("key", "secret", direction="DEPOSIT", start_time_ms=start_ms,
            end_time_ms=end_ms, page_size=2, max_pages=3)
        self.assertTrue(complete.complete)
        self.assertIsNone(complete.next_page)
        self.assertEqual(complete.request_count, 2)
        self.assertEqual(sum(len(page.items) for page in complete.pages), 3)

        transport.post_deposit_history = lambda body: (complete.pages[0] and {
            "code": 0, "method": "private/get-deposit-history", "result": {"deposit_list": [
                {"id": "cap-1", "currency": "BTC", "amount": "1", "fee": "0", "create_time": start_ms + 1,
                 "update_time": start_ms + 1, "status": "1"},
                {"id": "cap-2", "currency": "BTC", "amount": "1", "fee": "0", "create_time": start_ms + 2,
                 "update_time": start_ms + 2, "status": "1"}]}}, b"{}")
        capped = client.read_funding_history("key", "secret", direction="DEPOSIT", start_time_ms=start_ms,
            end_time_ms=end_ms, page_size=2, max_pages=1)
        self.assertFalse(capped.complete)
        self.assertEqual(capped.next_page, 1)

    def test_funding_history_rejects_invalid_amount_status_timestamps_and_bounds(self):
        start_ms = int((NOW.timestamp() - 3600) * 1000)
        end_ms = int((NOW.timestamp() + 3600) * 1000)
        valid = {"id": "dep-1", "currency": "BTC", "amount": "1", "fee": "0",
            "create_time": start_ms + 1, "update_time": start_ms + 1, "status": "1"}
        for change in ({"amount": "NaN"}, {"fee": "-0.1"}, {"status": "9"}, {"currency": "BTC_USDT"},
                       {"create_time": end_ms}, {"update_time": start_ms}):
            transport = FixtureTransport()
            transport.deposit_history_payload = {"code": 0, "method": "private/get-deposit-history", "result": {
                "deposit_list": [{**valid, **change}]}}
            with self.subTest(change=change), self.assertRaises(CryptoComPortfolioError):
                CryptoComReadOnlyPortfolioClient(transport, clock=lambda: NOW, nonce=lambda: 123456).read_funding_history(
                    "key", "secret", direction="DEPOSIT", start_time_ms=start_ms, end_time_ms=end_ms)

    def test_unified_wallet_assets_are_parsed_separately_from_margin_summary(self):
        payload = {"code": 0, "method": "private/user-balance", "result": {"data": [{
            "instrument_name": "USD", "total_available_balance": "800",
            "total_margin_balance": "900", "total_initial_margin": "100", "total_position_im": "0",
            "total_haircut": "100", "total_cash_balance": "1200",
            "total_isolated_cash_balance": "0", "is_liquidating": False,
            "position_balances": [{
                "instrument_name": "BTC", "quantity": "0.02", "market_value": "1200",
                "collateral_amount": "1140", "max_withdrawal_balance": "0.015",
                "reserved_qty": "0.001",
            }],
        }]}}
        snapshot = CryptoComReadOnlyPortfolioClient(
            FixtureTransport(payload), clock=lambda: NOW, nonce=lambda: 123456,
        ).read_balance("fixture-key", "fixture-secret")
        self.assertEqual(len(snapshot.balances), 1)
        self.assertEqual(snapshot.balances[0].available, Decimal("800"))
        self.assertEqual(snapshot.balances[0].total_cash, Decimal("1200"))
        self.assertEqual(snapshot.balances[0].total_position_im, Decimal("0"))
        self.assertEqual(snapshot.balances[0].total_haircut, Decimal("100"))
        self.assertEqual(len(snapshot.wallet_balances), 1)
        asset = snapshot.wallet_balances[0]
        self.assertEqual(asset.currency, "BTC")
        self.assertEqual(asset.account_currency, "USD")
        self.assertEqual(asset.quantity, Decimal("0.02"))
        self.assertEqual(asset.market_value, Decimal("1200"))
        self.assertEqual(asset.max_withdrawal_balance, Decimal("0.015"))
        self.assertEqual(asset.reserved_quantity, Decimal("0.001"))
        self.assertFalse(snapshot.usable_for_risk)

    def test_balance_requires_cash_and_validates_margin_risk_fields(self):
        base = {"instrument_name": "USD", "total_available_balance": "10",
                "total_margin_balance": "10", "total_initial_margin": "0",
                "total_cash_balance": "10", "total_isolated_cash_balance": "0", "is_liquidating": False}
        for change in ({"total_cash_balance": None}, {"total_isolated_cash_balance": None},
                       {"is_liquidating": None}, {"total_borrow": "NaN"},
                       {"is_liquidating": "false"}, {"isolated_positions": {} }):
            row = {**base, **change}
            payload = {"code": 0, "method": "private/user-balance", "result": {"data": [row]}}
            with self.subTest(change=change), self.assertRaises(CryptoComPortfolioError):
                CryptoComReadOnlyPortfolioClient(FixtureTransport(payload), clock=lambda: NOW,
                    nonce=lambda: 123456).read_balance("key", "secret")

    def test_nonzero_margin_liquidation_or_isolated_use_is_observed_explicitly(self):
        row = {"instrument_name": "USD", "total_available_balance": "10", "total_margin_balance": "10",
               "total_initial_margin": "0", "total_cash_balance": "10", "total_borrow": "2",
               "total_isolated_cash_balance": "1", "is_liquidating": True,
               "isolated_positions": [{"instrument_name": "BTCUSD-PERP"}]}
        payload = {"code": 0, "method": "private/user-balance", "result": {"data": [row]}}
        snapshot = CryptoComReadOnlyPortfolioClient(FixtureTransport(payload), clock=lambda: NOW,
            nonce=lambda: 123456).read_balance("key", "secret")
        account = snapshot.balances[0]
        self.assertEqual(account.total_borrow, Decimal("2"))
        self.assertEqual(account.isolated_cash, Decimal("1"))
        self.assertTrue(account.is_liquidating)
        self.assertTrue(account.has_isolated_positions)

    def test_exchange_transport_rejects_non_read_only_methods_before_network(self):
        from invest_ai.portfolio.cryptocom_readonly import CryptoComExchangeTransport

        with self.assertRaisesRegex(CryptoComPortfolioError, "not enabled by the read-only adapter"):
            CryptoComExchangeTransport()._post("private/create-order", b"{}")

    def test_transaction_history_is_signed_validated_and_remains_read_only(self):
        start_ms, end_ms = int((NOW.timestamp() - 3_600) * 1000), int((NOW.timestamp() + 3_600) * 1000)
        payload = {"code": 0, "method": "private/get-transactions", "result": {"data": [{
            "journal_type": "REALIZED_PNL", "journal_id": 1234, "transaction_qty": "0.5",
            "transaction_cost": "10.25", "realized_pnl": "2.5", "order_id": 456,
            "trade_id": 789, "instrument_name": "BTC_USDT", "side": "SELL",
            "event_timestamp_ms": str(start_ms + 1000),
        }]}}
        transport = FixtureTransport()
        transport.transactions_payload = payload
        client = CryptoComReadOnlyPortfolioClient(transport, clock=lambda: NOW, nonce=lambda: 123456)
        rows, digest = client.read_transactions("fixture-key", "fixture-secret", start_time_ms=start_ms,
            end_time_ms=end_ms, limit=10)
        request = json.loads(transport.transactions_body)
        params = request["params"]
        parameter_string = "".join(key + str(params[key]) for key in sorted(params))
        expected = hmac.new(b"fixture-secret",
            f"private/get-transactions123456fixture-key{parameter_string}123456".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(request["sig"], expected)
        self.assertEqual(request["method"], "private/get-transactions")
        self.assertEqual(rows[0].journal_type, "REALIZED_PNL")
        self.assertEqual(rows[0].realized_pnl, Decimal("2.5"))
        self.assertEqual(rows[0].order_id, "456")
        self.assertEqual(rows[0].instrument_name, "BTC_USDT")
        self.assertEqual(len(digest), 64)

    def test_transaction_history_rejects_unbounded_invalid_or_duplicate_pages(self):
        client = CryptoComReadOnlyPortfolioClient(FixtureTransport(), clock=lambda: NOW, nonce=lambda: 123456)
        start_ms, end_ms = int((NOW.timestamp() - 3_600) * 1000), int((NOW.timestamp() + 3_600) * 1000)
        for kwargs in ({"start_time_ms": end_ms, "end_time_ms": end_ms},
                       {"start_time_ms": start_ms, "end_time_ms": end_ms, "limit": 101}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CryptoComPortfolioError):
                client.read_transactions("key", "secret", **kwargs)
        transport = FixtureTransport()
        duplicate = {"journal_type": "TRADING", "journal_id": 99, "transaction_qty": "1",
            "transaction_cost": "1", "realized_pnl": "0", "instrument_name": "BTC_USDT",
            "event_timestamp_ms": start_ms + 1}
        transport.transactions_payload = {"code": 0, "method": "private/get-transactions",
            "result": {"data": [duplicate, duplicate]}}
        client = CryptoComReadOnlyPortfolioClient(transport, clock=lambda: NOW, nonce=lambda: 123456)
        with self.assertRaisesRegex(CryptoComPortfolioError, "identity or classification"):
            client.read_transactions("key", "secret", start_time_ms=start_ms, end_time_ms=end_ms)

    def test_transaction_history_accepts_documented_and_live_offchain_journal_types(self):
        start_ms = int((NOW.timestamp() - 3_600) * 1000)
        end_ms = int((NOW.timestamp() + 3_600) * 1000)
        documented = {
            "TRADING", "TRADE_FEE", "WITHDRAW_FEE", "WITHDRAW", "DEPOSIT",
            "ROLLBACK_DEPOSIT", "ROLLBACK_WITHDRAW", "FUNDING", "REALIZED_PNL",
            "INSURANCE_FUND", "SOCIALIZED_LOSS", "LIQUIDATION_FEE", "SESSION_RESET",
            "ADJUSTMENT", "SESSION_SETTLE", "UNCOVERED_LOSS", "ADMIN_ADJUSTMENT",
            "DELIST", "SETTLEMENT_FEE", "AUTO_CONVERSION", "MANUAL_CONVERSION",
            "OFFCHAIN_DEPOSIT", "OFFCHAIN_WITHDRAWAL",
        }
        for index, journal_type in enumerate(sorted(documented), 1):
            row = {
                "journal_type": journal_type, "journal_id": str(index),
                "transaction_qty": "0", "transaction_cost": "0", "realized_pnl": "0",
                "order_id": "", "trade_id": "", "instrument_name": "BTC_USDT",
                "side": "NULL_VAL", "event_timestamp_ms": start_ms + index,
            }
            with self.subTest(journal_type=journal_type):
                parsed = CryptoComReadOnlyPortfolioClient._parse_transactions(
                    [row], "a" * 64, start_ms, end_ms,
                )
                self.assertEqual(parsed[0].journal_type, journal_type)
                self.assertIsNone(parsed[0].side)
                self.assertIsNone(parsed[0].order_id)
                self.assertIsNone(parsed[0].trade_id)

    def test_effective_spot_fee_schedule_is_signed_and_validated(self):
        transport = FixtureTransport()
        client = CryptoComReadOnlyPortfolioClient(transport, clock=lambda: NOW, nonce=lambda: 123456)
        fees = client.read_fee_schedule("fixture-key", "fixture-secret")
        request = json.loads(transport.fee_body)
        expected = hmac.new(b"fixture-secret", b"private/get-fee-rate123456fixture-key123456",
            hashlib.sha256).hexdigest()
        self.assertEqual(request["sig"], expected)
        self.assertEqual(request["method"], "private/get-fee-rate")
        self.assertEqual(request["params"], {})
        self.assertEqual(fees.spot_tier, 5)
        self.assertEqual(fees.maker_rate_bps, Decimal("25"))
        self.assertEqual(fees.taker_rate_bps, Decimal("50"))
        self.assertEqual(fees.observed_at, NOW)
        for tier in ("not-a-tier", "1001", "5.0", True):
            transport.fee_payload["result"]["spot_tier"] = tier
            with self.subTest(tier=tier), self.assertRaisesRegex(
                    CryptoComPortfolioError, "invalid Spot fee tier"):
                client.read_fee_schedule("fixture-key", "fixture-secret")
        transport.fee_payload["result"]["spot_tier"] = "5"
        transport.fee_payload["result"]["effective_spot_taker_rate_bps"] = "-1"
        with self.assertRaisesRegex(CryptoComPortfolioError, "out-of-range"):
            client.read_fee_schedule("fixture-key", "fixture-secret")

    def test_signature_route_balances_and_non_authority_contract(self):
        transport = FixtureTransport()
        client = CryptoComReadOnlyPortfolioClient(transport, clock=lambda: NOW, nonce=lambda: 123456)
        snapshot = client.read_balance("fixture-key", "fixture-secret")
        request = json.loads(transport.body)
        expected = hmac.new(b"fixture-secret", b"private/user-balance123456fixture-key123456",
                            hashlib.sha256).hexdigest()
        self.assertEqual(request["id"], "123456")
        self.assertEqual(request["nonce"], "123456")
        self.assertEqual(request["method"], "private/user-balance")
        self.assertEqual(request["params"], {})
        self.assertEqual(request["sig"], expected)
        self.assertEqual([item.currency for item in snapshot.balances], ["EUR", "BTC"])
        self.assertEqual(snapshot.balances[0].available, Decimal("12.00"))
        self.assertEqual(snapshot.fetched_at, NOW)
        self.assertEqual(snapshot.source, "POST /private/user-balance")
        self.assertEqual(len(snapshot.payload_sha256), 64)
        self.assertFalse(snapshot.persistent)
        self.assertFalse(snapshot.usable_for_risk)
        self.assertFalse(snapshot.usable_for_execution)

    def test_read_portfolio_signs_all_read_only_routes_and_parses_positions_and_open_orders(self):
        positions = {"code": 0, "method": "private/get-positions", "result": {"data": [{
            "account_id": "fixture-account", "instrument_name": "BTCUSD-PERP",
            "type": "PERPETUAL_SWAP", "quantity": "0.25", "cost": "25000",
            "open_position_pnl": "125", "open_pos_cost": "24875", "session_pnl": "10",
            "update_timestamp_ms": "1791561600000",
        }]}}
        orders = {"code": 0, "method": "private/get-open-orders", "result": {"data": [{
            "account_id": "fixture-account", "order_id": 12345, "client_oid": "bubus-fixture-1",
            "instrument_name": "BTC_USDT", "side": "BUY", "order_type": "LIMIT", "status": "ACTIVE",
            "quantity": "0.01", "cumulative_quantity": "0.003", "limit_price": "60000",
            "create_time": 1791560000000, "update_time": 1791560100000,
        }]}}
        transport = FixtureTransport(positions_payload=positions, open_orders_payload=orders)
        client = CryptoComReadOnlyPortfolioClient(
            transport, clock=lambda: NOW, nonce=lambda: 123456,
        )
        snapshot = client.read_portfolio("fixture-key", "fixture-secret")
        balance_request = json.loads(transport.body)
        position_request = json.loads(transport.positions_body)
        order_request = json.loads(transport.open_orders_body)
        expected_sig = hmac.new(
            b"fixture-secret", b"private/get-positions123457fixture-key123457", hashlib.sha256,
        ).hexdigest()
        self.assertEqual(balance_request["method"], "private/user-balance")
        self.assertEqual(position_request["method"], "private/get-positions")
        self.assertEqual(position_request["id"], "123457")
        self.assertEqual(position_request["nonce"], "123457")
        self.assertEqual(position_request["sig"], expected_sig)
        expected_order_sig = hmac.new(
            b"fixture-secret", b"private/get-open-orders123458fixture-key123458", hashlib.sha256,
        ).hexdigest()
        self.assertEqual(order_request["method"], "private/get-open-orders")
        self.assertEqual(order_request["params"], {})
        self.assertEqual(order_request["sig"], expected_order_sig)
        self.assertEqual(len(snapshot.balances), 2)
        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(snapshot.positions[0].instrument_name, "BTCUSD-PERP")
        self.assertEqual(snapshot.positions[0].quantity, Decimal("0.25"))
        self.assertEqual(snapshot.positions[0].open_position_pnl_usd, Decimal("125"))
        self.assertEqual(snapshot.positions[0].updated_at, datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(len(snapshot.positions_payload_sha256), 64)
        self.assertEqual(len(snapshot.open_orders), 1)
        self.assertEqual(snapshot.open_orders[0].order_id, "12345")
        self.assertEqual(snapshot.open_orders[0].cumulative_quantity, Decimal("0.003"))
        self.assertEqual(snapshot.open_orders[0].quantity, Decimal("0.01"))
        self.assertEqual(snapshot.open_orders[0].limit_price, Decimal("60000"))
        self.assertEqual(len(snapshot.open_orders_payload_sha256), 64)
        self.assertFalse(snapshot.persistent)
        self.assertFalse(snapshot.usable_for_risk)
        self.assertFalse(snapshot.usable_for_execution)

    def test_read_portfolio_accepts_no_open_derivative_positions(self):
        snapshot = CryptoComReadOnlyPortfolioClient(
            FixtureTransport(), clock=lambda: NOW, nonce=lambda: 123456,
        ).read_portfolio("fixture-key", "fixture-secret")
        self.assertEqual(snapshot.positions, ())
        self.assertEqual(len(snapshot.balances), 2)

    def test_provider_error_and_wrong_method_fail_closed(self):
        for payload in ({"code": 100, "method": "private/user-balance", "result": {}},
                        {"code": 0, "method": "private/create-order", "result": {"data": []}}):
            with self.subTest(payload=payload), self.assertRaises(CryptoComPortfolioError):
                CryptoComReadOnlyPortfolioClient(
                    FixtureTransport(payload), clock=lambda: NOW, nonce=lambda: 123456,
                ).read_balance("key", "secret")

    def test_provider_error_reports_only_numeric_code(self):
        payload = {"code": 40101, "method": "private/user-balance",
                   "message": "do not surface provider text", "result": {}}
        with self.assertRaisesRegex(CryptoComPortfolioError, "40101") as raised:
            CryptoComReadOnlyPortfolioClient(
                FixtureTransport(payload), clock=lambda: NOW, nonce=lambda: 123456,
            ).read_balance("key", "sensitive-secret")
        self.assertNotIn("sensitive-secret", str(raised.exception))
        self.assertNotIn("do not surface provider text", str(raised.exception))

    def test_authentication_errors_give_specific_safe_guidance(self):
        cases = {
            40101: "cle complete et le secret",
            40103: "liste blanche IP",
            40104: "niveau du compte",
        }
        for code, expected in cases.items():
            payload = {"code": code, "method": "private/user-balance",
                       "message": "never expose provider text", "result": {}}
            with self.subTest(code=code), self.assertRaises(CryptoComPortfolioError) as raised:
                CryptoComReadOnlyPortfolioClient(
                    FixtureTransport(payload), clock=lambda: NOW, nonce=lambda: 123456,
                ).read_balance("fixture-key", "fixture-secret")
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn("fixture-secret", str(raised.exception))
            self.assertNotIn("never expose provider text", str(raised.exception))

    def test_transport_http_status_is_reported_without_details(self):
        class HttpErrorTransport:
            def post_balance(self, body):
                from io import BytesIO
                raise HTTPError("https://api.crypto.com/path", 401, "unauthorized", {},
                                BytesIO(b'{"code":40103,"original":"sensitive body"}'))

        with self.assertRaisesRegex(CryptoComPortfolioError, "HTTP 401, code API 40103") as raised:
            CryptoComReadOnlyPortfolioClient(
                HttpErrorTransport(), clock=lambda: NOW, nonce=lambda: 123456,
            ).read_balance("key", "sensitive-secret")
        self.assertNotIn("sensitive-secret", str(raised.exception))
        self.assertNotIn("sensitive body", str(raised.exception))

    def test_duplicate_currency_and_float_money_are_rejected(self):
        duplicate = {"code": 0, "method": "private/user-balance", "result": {"data": [
            {"instrument_name": "EUR", "total_available_balance": "1", "total_cash_balance": "1",
             "total_isolated_cash_balance": "0", "is_liquidating": False},
            {"instrument_name": "EUR", "total_available_balance": "2", "total_cash_balance": "2",
             "total_isolated_cash_balance": "0", "is_liquidating": False},
        ]}}
        float_amount = {"code": 0, "method": "private/user-balance", "result": {"data": [
            {"instrument_name": "EUR", "total_available_balance": "1.25", "total_cash_balance": 1.25,
             "total_isolated_cash_balance": "0", "is_liquidating": False},
        ]}}
        for payload in (duplicate, float_amount):
            with self.subTest(payload=payload), self.assertRaises(CryptoComPortfolioError):
                CryptoComReadOnlyPortfolioClient(
                    FixtureTransport(payload), clock=lambda: NOW, nonce=lambda: 123456,
                ).read_balance("key", "secret")

    def test_invalid_open_position_is_rejected(self):
        payload = {"code": 0, "method": "private/get-positions", "result": {"data": [{
            "instrument_name": "BTCUSD-PERP", "type": "PERPETUAL_SWAP", "quantity": "1",
            "cost": "100", "open_position_pnl": "0", "session_pnl": "0",
            "update_timestamp_ms": "not-a-time",
        }]}}
        with self.assertRaisesRegex(CryptoComPortfolioError, "timestamp is invalid"):
            CryptoComReadOnlyPortfolioClient(
                FixtureTransport(positions_payload=payload), clock=lambda: NOW, nonce=lambda: 123456,
            ).read_portfolio("key", "secret")

    def test_malformed_open_order_is_rejected_instead_of_omitted(self):
        for mutation in (
            {"side": ["BUY"]},
            {"quantity": "1", "cumulative_quantity": "2"},
            {"limit_price": "NaN"},
            {"account_id": ""},
        ):
            base = {"account_id": "fixture-account", "order_id": 42, "client_oid": "fixture-order",
                    "instrument_name": "BTC_USDT", "side": "BUY", "order_type": "LIMIT", "status": "ACTIVE",
                    "quantity": "1", "cumulative_quantity": "0", "limit_price": "60000"}
            base.update(mutation)
            payload = {"code": 0, "method": "private/get-open-orders", "result": {"data": [base]}}
            with self.subTest(mutation=mutation), self.assertRaises(CryptoComPortfolioError):
                CryptoComReadOnlyPortfolioClient(
                    FixtureTransport(open_orders_payload=payload), clock=lambda: NOW, nonce=lambda: 123456,
                ).read_open_orders("key", "secret")

    def test_credentials_required_and_timestamp_must_be_aware(self):
        with self.assertRaises(CryptoComPortfolioError):
            CryptoComReadOnlyPortfolioClient(nonce=lambda: 123456).read_balance("", "secret")
        with self.assertRaises(CryptoComPortfolioError):
            CryptoComReadOnlyPortfolioClient(FixtureTransport(), clock=lambda: datetime(2026, 1, 1),
                                             nonce=lambda: 123456).read_balance("key", "secret")


if __name__ == "__main__":
    unittest.main()
