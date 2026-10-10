"""Synthetic, read-only reconciliation evidence tests. No network or trading credentials."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bubus.reconciliation import ReconciliationEvidenceError, audit_reconciliation
from bubus.server import make_server
from bubus.store import DeskStore
from tests.assistant.test_market_dashboard import FakeCoinGeckoOverview, FakeCryptoOverview

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64


def valuation(at: str, identity: int, *, currency: str = "EUR") -> dict:
    return {"valuation_id": identity, "snapshot_id": "cryptocom-spot-candidate:" + _A,
        "as_of": at, "base_currency": currency, "total_equity": "100",
        "cash_value": "100", "spot_assets_value": "0",
        "held_asset_values": [{"asset": currency, "value": "100"}],
        "order_reservations": [], "market_snapshot_ids": [],
        "source_payload_sha256": [_B],
        "authority_status": "BROKER_SPOT_PORTFOLIO_CANDIDATE_ONLY",
        "usable_for_risk": False, "usable_for_execution": False, "orders_enabled": False,
        "persistent": True, "stored_payload_sha256": _C}


def event(journal_id: str, kind: str, at: str, *, quantity: str = "1") -> dict:
    return {"journal_type": kind, "journal_id": journal_id,
        "transaction_qty": quantity, "transaction_cost": "0.01",
        "realized_pnl": "0", "order_id": None, "trade_id": None,
        "instrument_name": "BTC_USDT", "side": None, "occurred_at": at,
        "source": "private/get-transactions", "payload_sha256": _A}


def page(identity: int, begin: str, end: str, events: list[dict]) -> dict:
    start_ms = int(datetime.fromisoformat(begin).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(end).timestamp() * 1000)
    return {"snapshot_id": identity, "observed_at": "2026-10-10T02:00:00+00:00",
        "start_time_ms": start_ms, "end_time_ms": end_ms, "item_count": len(events),
        "items": events, "provider_payload_sha256": _A, "payload_sha256": _B,
        "usable_for_risk": False, "orders_enabled": False}


def executed_trade(identity: str = "777", *, at: str = "2026-10-10T00:40:00+00:00",
                   order_id: str = "456", side: str = "BUY") -> dict:
    instant = datetime.fromisoformat(at)
    delta = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
    timestamp_ns = str((delta.days * 86400 + delta.seconds) * 1_000_000_000
                       + delta.microseconds * 1000)
    return {"trade_id": identity, "order_id": order_id, "trade_match_id": "1",
        "client_oid": "client-1", "instrument_name": "BTC_USDT", "side": side,
        "taker_side": "MAKER", "quantity": "0.01", "price": "60000",
        "fees": "-0.15", "fee_currency": "USDT", "occurred_at": instant.isoformat(),
        "timestamp_ns": timestamp_ns, "source": "private/get-trades", "payload_sha256": _A}


def trades_page(identity: int, begin: str, end: str, items: list[dict]) -> dict:
    start_ns = int((datetime.fromisoformat(begin) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1e9)
    end_ns = int((datetime.fromisoformat(end) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1e9)
    return {"snapshot_id": identity, "observed_at": "2026-10-10T02:00:00+00:00",
        "start_time_ns": start_ns, "end_time_ns": end_ns, "item_count": len(items),
        "items": items, "provider_payload_sha256": _A, "payload_sha256": _B,
        "usable_for_risk": False, "orders_enabled": False}


def complete_journal_page(identity: int, begin: str, end: str, items: list[dict]) -> dict:
    result = page(identity, begin, end, items)
    batch_id = str(uuid.uuid4())
    result["batch_id"] = batch_id
    result["batch"] = {"batch_id": batch_id, "query_start_time_ms": result["start_time_ms"],
        "query_end_time_ms": result["end_time_ms"], "coverage_complete": True,
        "incomplete_intervals": [], "request_count": 1, "page_count": 1}
    return result


def complete_execution_page(identity: int, begin: str, end: str, items: list[dict]) -> dict:
    result = trades_page(identity, begin, end, items)
    batch_id = str(uuid.uuid4())
    result["batch_id"] = batch_id
    result["batch"] = {"batch_id": batch_id, "query_start_time_ns": result["start_time_ns"],
        "query_end_time_ns": result["end_time_ns"], "coverage_complete": True,
        "incomplete_intervals": [], "request_count": 1, "page_count": 1}
    return result


def funding(direction: str, begin: str, end: str, *, status: str | None = None,
            page_size: int = 200) -> dict:
    start_ms = int(datetime.fromisoformat(begin).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(end).timestamp() * 1000)
    created = datetime.fromisoformat("2026-10-10T00:30:00+00:00").isoformat()
    source = "private/get-deposit-history" if direction == "DEPOSIT" else "private/get-withdrawal-history"
    items = [] if status is None else [{"direction": direction, "transfer_id": direction.lower()+"-1",
        "currency": "BTC", "amount": "0.01", "fee": "0.0001", "status": status,
        "created_at": created, "updated_at": created, "source": source, "payload_sha256": _A}]
    pages = [{"page": 0, "page_size": page_size, "items": items, "payload_sha256": _A}]
    evidence = {"direction": direction, "start_time_ms": start_ms, "end_time_ms": end_ms,
        "pages": pages, "complete": True, "next_page": None, "request_count": 1}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"snapshot_id": 1 if direction == "DEPOSIT" else 2,
        "observed_at": "2026-10-10T02:00:00+00:00", **evidence, "items": items,
        "item_count": len(items), "payload_sha256": digest, "authority_status": "OBSERVATION_ONLY",
        "usable_for_risk": False, "orders_enabled": False}


def candidates():
    return [valuation("2026-10-10T01:00:00+00:00", 2),
            valuation("2026-10-10T00:00:00+00:00", 1)]


def daily_observation_report(open_at: str) -> dict:
    close_at = "2026-10-10T01:00:00+00:00"
    rows = [valuation(close_at, 2), valuation(open_at, 1)]
    rows[0].update({"total_equity": "101", "cash_value": "101",
                    "held_asset_values": [{"asset": "EUR", "value": "101"}]})
    rows[1].update({"total_equity": "100", "cash_value": "100",
                    "held_asset_values": [{"asset": "EUR", "value": "100"}]})
    opening = portfolio_observation()
    opening.update({"snapshot_id": 20,
        "fetched_at": (datetime.fromisoformat(open_at) - timedelta(seconds=1)).isoformat(),
        "payload_sha256": "1" * 64, "positions_payload_sha256": "2" * 64,
        "open_orders_payload_sha256": "3" * 64,
        "wallet_balances": [wallet_balance("BTC", "0", "1" * 64),
            wallet_balance("USDT", "0", "1" * 64), wallet_balance("CRO", "0", "1" * 64)]})
    closing = portfolio_observation()
    closing.update({"snapshot_id": 21,
        "fetched_at": (datetime.fromisoformat(close_at) - timedelta(seconds=1)).isoformat(),
        "payload_sha256": "5" * 64, "positions_payload_sha256": "6" * 64,
        "open_orders_payload_sha256": "7" * 64,
        "wallet_balances": [wallet_balance("BTC", "0", "5" * 64),
            wallet_balance("USDT", "0", "5" * 64), wallet_balance("CRO", "0", "5" * 64)]})
    rows[1]["source_payload_sha256"] = ["1" * 64, "2" * 64, "3" * 64]
    rows[0]["source_payload_sha256"] = ["5" * 64, "6" * 64, "7" * 64]
    funding_rows = [funding("DEPOSIT", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00"),
        funding("WITHDRAWAL", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00")]
    return audit_reconciliation(valuations=rows,
        transaction_pages=[complete_journal_page(1, open_at, close_at, [])], portfolio=closing,
        portfolio_history=[closing, opening], cash_history=None,
        trade_pages=[complete_execution_page(1, open_at, close_at, [])], funding_history=funding_rows)


def portfolio_observation(open_orders: list[dict] | None = None) -> dict:
    return {"provider": "Crypto.com Exchange", "authority_status": "OBSERVATION_ONLY",
        "usable_for_risk": False, "usable_for_execution": False, "orders_enabled": False,
        "snapshot_id": 1, "stored_payload_sha256": "d" * 64,
        "payload_sha256": _A, "positions_payload_sha256": _B, "open_orders_payload_sha256": _C,
        "fetched_at": "2026-10-10T01:00:00+00:00", "positions": [], "wallet_balances": [],
        "open_orders": [] if open_orders is None else open_orders}


def open_order() -> dict:
    return {"account_id": "primary", "order_id": "42", "client_oid": "client-42",
        "instrument_name": "BTC_EUR", "side": "BUY", "order_type": "LIMIT", "status": "ACTIVE",
        "quantity": "0.01", "cumulative_quantity": "0", "limit_price": "60000",
        "created_at": "2026-10-10T00:30:00+00:00", "updated_at": "2026-10-10T00:30:00+00:00",
        "source": "private/get-open-orders", "payload_sha256": _C}


def wallet_balance(currency: str, quantity: str, payload_hash: str) -> dict:
    return {"account_currency": "USD", "collateral_amount": "10", "currency": currency,
        "market_value": "10", "max_withdrawal_balance": quantity, "payload_sha256": payload_hash,
        "quantity": quantity, "reserved_quantity": "0", "source": "private/user-balance"}


class ReconciliationTests(unittest.TestCase):
    def test_spot_trade_legs_reconcile_bound_opening_and_closing_wallet_balances(self):
        begin, end = "2026-10-10T00:00:00+00:00", "2026-10-10T01:00:00+00:00"
        rows = candidates()
        rows[0].update({"total_equity": "101", "cash_value": "101",
                        "held_asset_values": [{"asset": "EUR", "value": "101"}]})
        opening = portfolio_observation()
        opening.update({"snapshot_id": 20, "fetched_at": "2026-10-09T23:59:59+00:00",
            "payload_sha256": "1" * 64, "positions_payload_sha256": "2" * 64,
            "open_orders_payload_sha256": "3" * 64,
            "wallet_balances": [wallet_balance("BTC", "0", "1" * 64),
                wallet_balance("USDT", "1000", "1" * 64), wallet_balance("CRO", "1", "1" * 64)]})
        closing = portfolio_observation()
        closing.update({"snapshot_id": 21, "fetched_at": "2026-10-10T00:59:59+00:00",
            "payload_sha256": "5" * 64, "positions_payload_sha256": "6" * 64,
            "open_orders_payload_sha256": "7" * 64,
            "wallet_balances": [wallet_balance("BTC", "0.01", "5" * 64),
                wallet_balance("USDT", "399.85", "5" * 64), wallet_balance("CRO", "1", "5" * 64)]})
        rows[1]["source_payload_sha256"] = ["1" * 64, "2" * 64, "3" * 64]
        rows[0]["source_payload_sha256"] = ["5" * 64, "6" * 64, "7" * 64]
        journal = event("55", "TRADING", "2026-10-10T00:40:00+00:00")
        journal.update({"trade_id": "777", "order_id": "456", "side": "BUY"})
        funding_rows = [funding("DEPOSIT", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00"),
            funding("WITHDRAWAL", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00")]
        report = audit_reconciliation(valuations=rows,
            transaction_pages=[complete_journal_page(1, begin, end, [journal])],
            portfolio=closing, portfolio_history=[closing, opening], cash_history=None,
            trade_pages=[complete_execution_page(1, begin, end, [executed_trade()])],
            funding_history=funding_rows)
        self.assertTrue(report["executed_trades_reconciled"])
        self.assertTrue(report["per_asset_postings_reconciled"])
        self.assertTrue(report["fee_postings_reconciled"])
        self.assertEqual(report["asset_ledger"]["status"], "MATCHED_ARITHMETIC_ONLY")
        self.assertEqual(report["asset_ledger"]["posting_count"], 3)
        self.assertNotIn("PER_ASSET_POSTINGS_MISSING", report["reasons"])
        self.assertNotIn("FEE_CURRENCY_AND_POSTINGS_MISSING", report["reasons"])
        self.assertIn("PNL_NOT_COMPUTED", report["reasons"])
        self.assertEqual(report["period_pnl"]["amount"], "1")
        self.assertEqual(report["period_pnl"]["currency"], "EUR")
        self.assertFalse(report["period_pnl"]["daily_pnl"])
        self.assertEqual(report["daily_pnl_observation"]["amount"], "1")
        self.assertEqual(report["daily_pnl_observation"]["authority_status"],
                         "RECONCILED_UTC_DAY_OBSERVATION_ONLY")
        self.assertTrue(report["daily_pnl_observation"]["daily_pnl"])
        self.assertFalse(report["daily_pnl_observation"]["usable_for_risk"])
        self.assertFalse(report["daily_pnl_observation"]["orders_enabled"])
        self.assertIsNone(report["pnl"])
        self.assertFalse(report["usable_for_risk"])
        self.assertFalse(report["orders_enabled"])

        closing["wallet_balances"][1]["quantity"] = "399.84"
        mismatch = audit_reconciliation(valuations=rows,
            transaction_pages=[complete_journal_page(2, begin, end, [journal])],
            portfolio=closing, portfolio_history=[closing, opening], cash_history=None,
            trade_pages=[complete_execution_page(2, begin, end, [executed_trade()])],
            funding_history=funding_rows)
        self.assertFalse(mismatch["per_asset_postings_reconciled"])
        self.assertEqual(mismatch["asset_ledger"]["status"], "BLOCKED_CONTRADICTORY")
        self.assertIsNone(mismatch["period_pnl"])
        self.assertIsNone(mismatch["daily_pnl_observation"])
        self.assertIn("PER_ASSET_POSTINGS_MISSING", mismatch["reasons"])
        self.assertIsNone(mismatch["pnl"])

    def test_period_observation_is_not_extrapolated_into_daily_pnl(self):
        report = audit_reconciliation(valuations=candidates(), transaction_pages=[],
            portfolio=None, cash_history=None)
        self.assertIsNone(report["daily_pnl_observation"])
        self.assertIsNone(report["pnl"])
        self.assertFalse(report["usable_for_risk"])
        self.assertFalse(report["orders_enabled"])

    def test_daily_observation_rejects_a_pre_midnight_opening_valuation(self):
        report = daily_observation_report("2026-10-09T23:59:50+00:00")
        self.assertIsNone(report["daily_pnl_observation"])
        self.assertIsNone(report["pnl"])
        self.assertFalse(report["usable_for_risk"])
        self.assertFalse(report["orders_enabled"])

    def test_daily_observation_accepts_opening_valuation_within_30_seconds_after_midnight(self):
        report = daily_observation_report("2026-10-10T00:00:20+00:00")
        self.assertEqual(report["daily_pnl_observation"]["amount"], "1")
        self.assertEqual(report["daily_pnl_observation"]["start"], "2026-10-10T00:00:20+00:00")
        self.assertFalse(report["daily_pnl_observation"]["usable_for_risk"])
        self.assertFalse(report["daily_pnl_observation"]["orders_enabled"])

    def test_complete_trade_sources_reconcile_empty_and_matching_trade_windows(self):
        begin, end = "2026-10-10T00:00:00+00:00", "2026-10-10T01:00:00+00:00"
        funding_rows = [funding("DEPOSIT", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00"),
            funding("WITHDRAWAL", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00")]
        transactions = complete_journal_page(1, begin, end, [])
        executions = complete_execution_page(1, begin, end, [])
        empty_report = audit_reconciliation(valuations=candidates(), transaction_pages=[transactions],
            portfolio=None, cash_history=None, trade_pages=[executions], funding_history=funding_rows)
        self.assertTrue(empty_report["executed_trades_reconciled"])
        self.assertTrue(empty_report["conversion_legs_reconciled"])
        self.assertTrue(empty_report["fee_postings_reconciled"])
        self.assertNotIn("EXECUTED_TRADES_NOT_FULLY_RECONCILED", empty_report["reasons"])
        self.assertNotIn("CONVERSION_LEGS_UNMATCHED", empty_report["reasons"])
        self.assertNotIn("FEE_CURRENCY_AND_POSTINGS_MISSING", empty_report["reasons"])
        self.assertFalse(empty_report["usable_for_risk"])

        journal = event("55", "TRADING", "2026-10-10T00:40:00+00:00")
        journal.update({"trade_id": "777", "order_id": "456", "side": "BUY"})
        transactions = complete_journal_page(2, begin, end, [journal])
        executions = complete_execution_page(2, begin, end, [executed_trade()])
        matched_report = audit_reconciliation(valuations=candidates(), transaction_pages=[transactions],
            portfolio=None, cash_history=None, trade_pages=[executions], funding_history=funding_rows)
        self.assertTrue(matched_report["executed_trades_reconciled"])
        self.assertFalse(matched_report["fee_postings_reconciled"])
        self.assertEqual(matched_report["matched_trade_journal_references"], 1)
        self.assertNotIn("EXECUTED_TRADES_NOT_FULLY_RECONCILED", matched_report["reasons"])
        self.assertIn("FEE_CURRENCY_AND_POSTINGS_MISSING", matched_report["reasons"])
        self.assertFalse(matched_report["usable_for_risk"])

    def test_complete_trade_sources_reject_a_trade_missing_from_transaction_journal(self):
        begin, end = "2026-10-10T00:00:00+00:00", "2026-10-10T01:00:00+00:00"
        transactions = complete_journal_page(1, begin, end, [])
        executions = complete_execution_page(1, begin, end, [executed_trade()])
        report = audit_reconciliation(valuations=candidates(), transaction_pages=[transactions],
            portfolio=None, cash_history=None, trade_pages=[executions])
        self.assertFalse(report["executed_trades_reconciled"])
        self.assertIn("EXECUTED_TRADES_NOT_FULLY_RECONCILED", report["reasons"])

    def test_historical_valuation_binds_to_its_own_account_snapshot_not_a_later_refresh(self):
        rows = candidates()
        opening = portfolio_observation()
        opening.update({"snapshot_id": 20, "fetched_at": "2026-10-09T23:59:59+00:00",
            "payload_sha256": "1" * 64, "positions_payload_sha256": "2" * 64,
            "open_orders_payload_sha256": "3" * 64, "stored_payload_sha256": "4" * 64,
            "wallet_balances": [wallet_balance("BTC", "0.01", "1" * 64)]})
        closing = portfolio_observation()
        closing.update({"snapshot_id": 21, "fetched_at": "2026-10-10T00:59:59+00:00",
            "payload_sha256": "5" * 64, "positions_payload_sha256": "6" * 64,
            "open_orders_payload_sha256": "7" * 64, "stored_payload_sha256": "8" * 64,
            "wallet_balances": [wallet_balance("BTC", "0.01", "5" * 64)]})
        rows[1]["source_payload_sha256"] = ["1" * 64, "2" * 64, "3" * 64]
        rows[0]["source_payload_sha256"] = ["5" * 64, "6" * 64, "7" * 64]
        later = portfolio_observation()
        later.update({"snapshot_id": 22, "fetched_at": "2026-10-10T01:30:00+00:00"})

        begin, end = "2026-10-10T00:00:00+00:00", "2026-10-10T01:00:00+00:00"
        funding_rows = [funding("DEPOSIT", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00"),
            funding("WITHDRAWAL", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00")]
        report = audit_reconciliation(valuations=rows,
            transaction_pages=[complete_journal_page(1, begin, end, [])], portfolio=later,
            portfolio_history=[later, closing, opening], cash_history=None,
            trade_pages=[complete_execution_page(1, begin, end, [])], funding_history=funding_rows)

        self.assertTrue(report["open_orders_bound_to_latest_valuation"])
        self.assertEqual(report["evidence"]["portfolio"]["valuation_source_match_snapshot_id"], 21)
        self.assertNotIn("PORTFOLIO_VALUATION_SOURCE_MISMATCH", report["reasons"])
        self.assertNotIn("OPENING_CLOSING_BALANCES_UNPROVEN", report["reasons"])
        self.assertTrue(report["per_asset_postings_reconciled"])
        self.assertNotIn("PER_ASSET_POSTINGS_MISSING", report["reasons"])
        self.assertFalse(report["usable_for_risk"])
        self.assertFalse(report["orders_enabled"])

    def test_historical_valuation_binding_rejects_account_snapshot_after_valuation(self):
        rows = candidates()
        after = portfolio_observation()
        after.update({"snapshot_id": 30, "fetched_at": "2026-10-10T01:00:01+00:00",
            "payload_sha256": "1" * 64, "positions_payload_sha256": "2" * 64,
            "open_orders_payload_sha256": "3" * 64})
        rows[0]["source_payload_sha256"] = ["1" * 64, "2" * 64, "3" * 64]
        report = audit_reconciliation(valuations=rows, transaction_pages=[], portfolio=after,
            portfolio_history=[after], cash_history=None)
        self.assertIn("PORTFOLIO_VALUATION_SOURCE_MISMATCH", report["reasons"])
        self.assertIn("OPENING_CLOSING_BALANCES_UNPROVEN", report["reasons"])
        self.assertFalse(report["open_orders_bound_to_latest_valuation"])

    def test_missing_sources_fail_closed_without_fake_pnl(self):
        result = audit_reconciliation(valuations=[], transaction_pages=[], portfolio=None, cash_history=None)
        self.assertEqual(result["status"], "BLOCKED_INCOMPLETE")
        self.assertIn("TWO_VALUATIONS_REQUIRED", result["reasons"])
        self.assertIn("TRANSACTION_HISTORY_MISSING", result["reasons"])
        self.assertIn("PNL_NOT_COMPUTED", result["reasons"])
        self.assertNotIn("CASH_HISTORY_MISSING", result["reasons"])
        for name in ("usable_for_pnl", "usable_for_risk", "usable_for_execution", "orders_enabled"):
            self.assertIs(result[name], False)
        for name in ("pnl", "net_deposits", "net_withdrawals", "net_conversions", "net_fees"):
            self.assertIsNone(result[name])
        self.assertEqual(result, audit_reconciliation(
            valuations=[], transaction_pages=[], portfolio=None, cash_history=None))
        self.assertEqual(len(result["evidence_sha256"]), 64)

    def test_complete_terminal_funding_histories_cover_valuation_window_but_do_not_enable_risk(self):
        histories = [
            funding("DEPOSIT", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00", status="1"),
            funding("WITHDRAWAL", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00"),
        ]
        report = audit_reconciliation(valuations=candidates(), transaction_pages=[], portfolio=None,
            cash_history=None, funding_history=histories)
        self.assertTrue(report["funding_history_coverage_verified"])
        self.assertEqual(report["funding_history_counts"]["completed_deposits"], 1)
        self.assertNotIn("DEPOSIT_WITHDRAWAL_PROOF_MISSING", report["reasons"])
        self.assertIn("PER_ASSET_POSTINGS_MISSING", report["reasons"])
        self.assertFalse(report["usable_for_risk"])
        self.assertFalse(report["orders_enabled"])
        self.assertIsNone(report["pnl"])

    def test_funding_coverage_selects_older_full_window_over_newer_partial_reads(self):
        funding_rows = [
            funding("DEPOSIT", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00", status="1"),
            funding("WITHDRAWAL", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00"),
            funding("DEPOSIT", "2026-10-10T00:10:00+00:00", "2026-10-10T00:50:00+00:00"),
            funding("WITHDRAWAL", "2026-10-10T00:10:00+00:00", "2026-10-10T00:50:00+00:00"),
        ]
        report = audit_reconciliation(valuations=candidates(), transaction_pages=[], portfolio=None,
            cash_history=None, funding_history=funding_rows)
        self.assertTrue(report["funding_history_coverage_verified"])
        self.assertEqual(report["funding_history_coverage_snapshot_ids"], {"DEPOSIT": 1, "WITHDRAWAL": 2})
        self.assertEqual(report["funding_history_counts"]["deposits"], 1)
        self.assertNotIn("DEPOSIT_WITHDRAWAL_PROOF_MISSING", report["reasons"])
        self.assertFalse(report["usable_for_risk"])
        self.assertFalse(report["orders_enabled"])

    def test_funding_histories_with_gaps_pending_status_or_tampering_do_not_prove_flows(self):
        histories = [
            funding("DEPOSIT", "2026-10-10T00:10:00+00:00", "2026-10-10T01:10:00+00:00", status="3"),
            funding("WITHDRAWAL", "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00"),
        ]
        report = audit_reconciliation(valuations=candidates(), transaction_pages=[], portfolio=None,
            cash_history=None, funding_history=histories)
        self.assertFalse(report["funding_history_coverage_verified"])
        self.assertIn("FUNDING_HISTORY_WINDOW_GAPS", report["reasons"])
        self.assertIn("DEPOSIT_WITHDRAWAL_PROOF_MISSING", report["reasons"])
        histories[0]["items"][0]["amount"] = "999"
        with self.assertRaises(ReconciliationEvidenceError):
            audit_reconciliation(valuations=candidates(), transaction_pages=[], portfolio=None,
                cash_history=None, funding_history=histories)

    def test_open_orders_must_be_bound_to_latest_valuation_and_reservations(self):
        order = open_order()
        account = portfolio_observation([order])
        rows = candidates()
        rows[0]["source_payload_sha256"] = [_A, _B, _C]
        rows[0]["order_reservations"] = [{"order_id": "42", "value": "600"}]
        report = audit_reconciliation(valuations=rows, transaction_pages=[], portfolio=account,
            cash_history=None)
        self.assertEqual(report["open_order_count"], 1)
        self.assertTrue(report["open_orders_bound_to_latest_valuation"])
        self.assertNotIn("OPEN_ORDER_RESERVATIONS_MISMATCH", report["reasons"])
        self.assertFalse(report["usable_for_risk"])
        self.assertFalse(report["orders_enabled"])

        rows[0]["order_reservations"] = []
        mismatched = audit_reconciliation(valuations=rows, transaction_pages=[], portfolio=account,
            cash_history=None)
        self.assertFalse(mismatched["open_orders_bound_to_latest_valuation"])
        self.assertIn("OPEN_ORDER_RESERVATIONS_MISMATCH", mismatched["reasons"])

    def test_open_order_binding_rejects_stale_source_or_invalid_exchange_row(self):
        account = portfolio_observation([open_order()])
        rows = candidates()
        rows[0]["source_payload_sha256"] = [_A, _B, _C]
        rows[0]["order_reservations"] = [{"order_id": "42", "value": "600"}]
        account["open_orders_payload_sha256"] = "e" * 64
        report = audit_reconciliation(valuations=rows, transaction_pages=[], portfolio=account,
            cash_history=None)
        self.assertIn("OPEN_ORDERS_OBSERVATION_INVALID", report["reasons"])
        self.assertIn("OPEN_ORDERS_OBSERVATION_MISSING", report["reasons"])
        self.assertFalse(report["open_orders_bound_to_latest_valuation"])

        account = portfolio_observation([dict(open_order(), status="FILLED")])
        report = audit_reconciliation(valuations=rows, transaction_pages=[], portfolio=account,
            cash_history=None)
        self.assertIn("OPEN_ORDERS_OBSERVATION_INVALID", report["reasons"])

    def test_executed_trades_crosscheck_journal_identity_without_claiming_pnl(self):
        journal = event("55", "TRADING", "2026-10-10T00:40:00+00:00")
        journal.update({"trade_id": "777", "order_id": "456", "side": "BUY"})
        trades = [trades_page(1, "2026-10-10T00:00:00+00:00", "2026-10-10T01:00:00+00:00",
            [executed_trade()])]
        report = audit_reconciliation(valuations=candidates(),
            transaction_pages=[page(1, "2026-10-10T00:00:00+00:00", "2026-10-10T01:00:00+00:00", [journal])],
            portfolio=None, cash_history=None, trade_pages=trades)
        self.assertEqual(report["executed_trade_count"], 1)
        self.assertEqual(report["matched_trade_journal_references"], 1)
        self.assertEqual(report["unmatched_journal_trade_references"], 0)
        self.assertEqual(report["trade_coverage_gaps"], [])
        self.assertEqual(report["status"], "BLOCKED_INCOMPLETE")
        self.assertFalse(report["usable_for_pnl"])
        self.assertIsNone(report["pnl"])

    def test_trade_identity_conflicts_and_truncated_pages_remain_blocked(self):
        journal = event("55", "TRADING", "2026-10-10T00:40:00+00:00")
        journal.update({"trade_id": "777", "order_id": "999", "side": "BUY"})
        page_rows = [executed_trade(str(index)) for index in range(100)]
        page_rows[0] = executed_trade()
        trades = [trades_page(1, "2026-10-10T00:00:00+00:00", "2026-10-10T01:00:00+00:00", page_rows)]
        report = audit_reconciliation(valuations=candidates(),
            transaction_pages=[page(1, "2026-10-10T00:00:00+00:00", "2026-10-10T01:00:00+00:00", [journal])],
            portfolio=None, cash_history=None, trade_pages=trades)
        self.assertEqual(report["status"], "BLOCKED_CONTRADICTORY")
        self.assertIn("TRADE_JOURNAL_IDENTITY_CONFLICT", report["reasons"])
        self.assertIn("TRADE_PAGE_AT_LIMIT", report["reasons"])
        self.assertIsNone(report["pnl"])

    def test_malformed_trade_provenance_or_amounts_are_rejected(self):
        valid = executed_trade()
        for bad in (dict(valid, fees="NaN"), dict(valid, payload_sha256=_C),
                    dict(valid, timestamp_ns="not-a-timestamp"), dict(valid, occurred_at="2026-10-10T00:40:00")):
            with self.subTest(bad=bad):
                with self.assertRaises(ReconciliationEvidenceError):
                    audit_reconciliation(valuations=[], transaction_pages=[], portfolio=None,
                        cash_history=None, trade_pages=[trades_page(1,
                            "2026-10-10T00:00:00+00:00", "2026-10-10T01:00:00+00:00", [bad])])

    def test_covered_pages_classify_without_summing_or_assuming_completeness(self):
        pages = [
            page(2, "2026-10-10T00:30:00+00:00", "2026-10-10T01:30:00+00:00", [
                event("12", "TRADING", "2026-10-10T00:40:00+00:00"),
                event("13", "TRADE_FEE", "2026-10-10T00:50:00+00:00")]),
            page(1, "2026-10-09T23:30:00+00:00", "2026-10-10T00:45:00+00:00", [
                event("11", "OFFCHAIN_DEPOSIT", "2026-10-10T00:10:00+00:00"),
                event("12", "TRADING", "2026-10-10T00:40:00+00:00")]),
        ]
        result = audit_reconciliation(valuations=candidates(), transaction_pages=pages,
                                      portfolio=None, cash_history=None)
        self.assertEqual(result["status"], "BLOCKED_INCOMPLETE")
        self.assertEqual(result["event_counts"]["deposits"], 1)
        self.assertEqual(result["event_counts"]["trades"], 1)
        self.assertEqual(result["event_counts"]["fees"], 1)
        self.assertEqual(result["distinct_journals"], 3)
        self.assertEqual(result["coverage_gaps"], [])
        self.assertNotIn("TRANSACTION_WINDOW_GAPS", result["reasons"])
        self.assertIn("COMPLETE_PROVIDER_PAGINATION_UNPROVEN", result["reasons"])
        self.assertIsNone(result["pnl"])
        self.assertEqual(result["valuation_pair"]["base_currency"], "EUR")

    def test_complete_provider_batches_prove_pagination_only_for_their_covered_window(self):
        tx_batch_id, trade_batch_id = str(uuid.uuid4()), str(uuid.uuid4())
        tx_start = int(datetime.fromisoformat("2026-10-09T23:00:00+00:00").timestamp() * 1000)
        tx_end = int(datetime.fromisoformat("2026-10-10T02:00:00+00:00").timestamp() * 1000)
        transaction = page(1, "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00", [])
        transaction.update({"batch_id": tx_batch_id, "batch": {
            "batch_id": tx_batch_id, "query_start_time_ms": tx_start, "query_end_time_ms": tx_end,
            "coverage_complete": True, "incomplete_intervals": [], "request_count": 1, "page_count": 1}})
        trade_start = tx_start * 1_000_000
        trade_end = tx_end * 1_000_000
        trades = trades_page(1, "2026-10-09T23:00:00+00:00", "2026-10-10T02:00:00+00:00", [])
        trades.update({"batch_id": trade_batch_id, "batch": {
            "batch_id": trade_batch_id, "query_start_time_ns": trade_start,
            "query_end_time_ns": trade_end, "coverage_complete": True,
            "incomplete_intervals": [], "request_count": 1, "page_count": 1}})
        result = audit_reconciliation(valuations=candidates(), transaction_pages=[transaction],
            portfolio=None, cash_history=None, trade_pages=[trades])
        self.assertTrue(result["provider_pagination_verified"])
        self.assertNotIn("COMPLETE_PROVIDER_PAGINATION_UNPROVEN", result["reasons"])
        self.assertFalse(result["usable_for_risk"])
        self.assertFalse(result["orders_enabled"])

    def test_conflicting_event_and_valuation_currency_are_reported(self):
        pages = [
            page(2, "2026-10-10T00:00:00+00:00", "2026-10-10T02:00:00+00:00", [
                event("12", "TRADING", "2026-10-10T00:40:00+00:00", quantity="1")]),
            page(1, "2026-10-10T00:00:00+00:00", "2026-10-10T02:00:00+00:00", [
                event("12", "TRADING", "2026-10-10T00:40:00+00:00", quantity="2")]),
        ]
        valuations = candidates()
        valuations[0]["base_currency"] = "USD"
        result = audit_reconciliation(valuations=valuations, transaction_pages=pages,
                                      portfolio=None, cash_history=None)
        self.assertEqual(result["status"], "BLOCKED_CONTRADICTORY")
        self.assertIn("CONFLICTING_JOURNAL_ID", result["reasons"])
        self.assertIn("VALUATION_CURRENCY_MISMATCH", result["reasons"])
        self.assertIsNone(result["valuation_pair"])
        self.assertFalse(result["usable_for_execution"])

    def test_different_user_selected_valuation_currency_is_incomparable_not_a_false_conflict(self):
        rows = candidates()
        rows[0]["base_currency"] = "USD"
        report = audit_reconciliation(valuations=rows, transaction_pages=[],
                                      portfolio=None, cash_history=None)
        self.assertEqual(report["status"], "BLOCKED_INCOMPLETE")
        self.assertIn("VALUATION_CURRENCY_MISMATCH", report["reasons"])
        self.assertIsNone(report["valuation_pair"])
        self.assertIsNone(report["pnl"])

    def test_interval_gaps_and_truncation_are_explicit(self):
        events = [event(str(i + 1), "MANUAL_CONVERSION", "2026-10-10T00:05:00+00:00")
                  for i in range(100)]
        result = audit_reconciliation(valuations=candidates(), transaction_pages=[
            page(1, "2026-10-10T00:00:00+00:00", "2026-10-10T00:30:00+00:00", events)],
            portfolio=None, cash_history=None)
        self.assertIn("TRANSACTION_PAGE_AT_LIMIT", result["reasons"])
        self.assertIn("TRANSACTION_WINDOW_GAPS", result["reasons"])
        self.assertEqual(result["event_counts"]["conversions"], 100)
        self.assertEqual(len(result["coverage_gaps"]), 1)
        self.assertEqual(result["status"], "BLOCKED_INCOMPLETE")

    def test_malformed_and_contradictory_journals_are_rejected(self):
        valid = event("1", "TRADING", "2026-10-10T00:10:00+00:00")
        for bad in [
            dict(valid, transaction_qty="NaN"),
            dict(valid, transaction_cost="Infinity"),
            dict(valid, occurred_at="2026-10-10T00:10:00"),
            dict(valid, occurred_at="2026-10-11T00:10:00+00:00"),
            dict(valid, journal_id="not-an-id"),
            dict(valid, usable_for_risk=True),
        ]:
            with self.subTest(bad=bad):
                with self.assertRaises(ReconciliationEvidenceError):
                    audit_reconciliation(valuations=[], transaction_pages=[
                        page(1, "2026-10-10T00:00:00+00:00", "2026-10-10T01:00:00+00:00", [bad])],
                        portfolio=None, cash_history=None)


class ReconciliationRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(0, data_dir=self.temp.name,
            cryptocom_market_overview=FakeCryptoOverview(),
            coingecko_market_overview=FakeCoinGeckoOverview())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def test_route_is_local_and_fail_closed_on_corrupt_stored_page(self):
        with urlopen(self.url + "/api/desk/reconciliation") as response:
            initial = json.load(response)["reconciliation"]
        self.assertEqual(initial["status"], "BLOCKED_INCOMPLETE")
        self.assertFalse(initial["usable_for_risk"])
        with self.assertRaises(HTTPError) as denied:
            urlopen(Request(self.url + "/api/desk/reconciliation",
                            headers={"Origin": "https://another.example"}))
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()

        store = self.server.desk_store
        now = datetime.now(timezone.utc)
        store.record_cryptocom_transactions({
            "observed_at": now.isoformat(),
            "start_time_ms": 1, "end_time_ms": 10,
            "items": [], "provider_payload_sha256": _A,
        })
        with urlopen(self.url + "/api/desk/reconciliation") as response:
            clean = json.load(response)["reconciliation"]
        self.assertEqual(len(clean["evidence"]["transaction_pages"]), 1)
        db = sqlite3.connect(Path(self.temp.name) / "desk.sqlite3")
        try:
            db.execute("UPDATE cryptocom_transaction_snapshots SET payload_json='{}'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(HTTPError) as refused:
            urlopen(self.url + "/api/desk/reconciliation")
        self.assertEqual(refused.exception.code, 503)
        refused.exception.close()

    def test_api_rejects_rehashed_semantically_invalid_event(self):
        store = self.server.desk_store
        store.record_cryptocom_transactions({
            "observed_at": "2026-10-10T02:00:00+00:00",
            "start_time_ms": int(datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp() * 1000),
            "end_time_ms": int(datetime(2026, 10, 10, 1, tzinfo=timezone.utc).timestamp() * 1000),
            "items": [event("123", "TRADING", "2026-10-10T00:10:00+00:00")],
            "provider_payload_sha256": _A,
        })
        db = sqlite3.connect(Path(self.temp.name) / "desk.sqlite3")
        try:
            raw, = db.execute("SELECT payload_json FROM cryptocom_transaction_snapshots").fetchone()
            payload = json.loads(raw)
            payload["items"][0]["transaction_qty"] = "NaN"
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            import hashlib
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            db.execute("UPDATE cryptocom_transaction_snapshots SET payload_json=?,payload_sha256=?",
                       (encoded, digest))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(HTTPError) as refused:
            urlopen(self.url + "/api/desk/reconciliation")
        self.assertEqual(refused.exception.code, 503)
        refused.exception.close()


if __name__ == "__main__":
    unittest.main()
