from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from invest_ai.data.cryptocom_market_readonly import CryptoComMarketEvidenceStore, CryptoComMarketTick
from invest_ai.data.snapshot_store import MarketSnapshotStore
from invest_ai.portfolio.cryptocom_candidate import CryptoComCandidateError, evaluate_spot_portfolio_candidate
from invest_ai.portfolio.cryptocom_readonly import (
    CryptoComBalance, CryptoComOpenOrder, CryptoComPortfolioSnapshot, CryptoComPosition, CryptoComWalletBalance,
)


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def portfolio(*, positions=(), open_orders=(), borrow=Decimal("0"), initial_margin=Decimal("0"),
              position_im=None, haircut=None, isolated=Decimal("0"), liquidating=False, isolated_positions=False):
    digest = "a" * 64
    balances = (CryptoComBalance("USD", Decimal("1000"), Decimal("1000"), initial_margin,
        Decimal("1100"), borrow, isolated, liquidating, isolated_positions, "private/user-balance", digest,
        position_im, haircut),)
    wallets = (
        CryptoComWalletBalance("USD", "USD", Decimal("500"), Decimal("500"), Decimal("0"),
            Decimal("500"), Decimal("0"), "private/user-balance", digest),
        CryptoComWalletBalance("BTC", "USD", Decimal("0.01"), Decimal("600"), Decimal("0"),
            Decimal("0.01"), Decimal("0.002"), "private/user-balance", digest),
    )
    return CryptoComPortfolioSnapshot(NOW, balances, digest, tuple(positions), digest, wallets,
        tuple(open_orders), digest)


def btc_market(root, *, price=Decimal("60000"), observed_at=NOW):
    store = CryptoComMarketEvidenceStore(root, clock=lambda: NOW)
    tick = CryptoComMarketTick("BTC_USD", price, price - Decimal("1"), price + Decimal("1"), "COMPLETE",
        observed_at, observed_at, "USD", "Crypto.com Exchange", "Spot", "ticker", "b" * 64)
    record = store.record(tick)
    return MarketSnapshotStore(root), {"BTC_USD": record.snapshot_id}


class CryptoComCandidateTests(unittest.TestCase):
    def test_prices_itemized_wallet_once_and_keeps_order_reservation_out_of_equity(self):
        with tempfile.TemporaryDirectory() as root:
            store, refs = btc_market(root)
            order = CryptoComOpenOrder("account", "42", None, "BTC_USD", "BUY", "LIMIT", "ACTIVE",
                Decimal("0.01"), Decimal("0.002"), Decimal("59000"), NOW, NOW,
                "private/get-open-orders", "c" * 64)
            candidate = evaluate_spot_portfolio_candidate(portfolio(open_orders=(order,)), refs, store,
                base_currency="USD", as_of=NOW, max_age_seconds=30)
        self.assertEqual(candidate.cash_value, Decimal("500"))
        self.assertEqual(candidate.spot_assets_value, Decimal("600"))
        self.assertEqual(candidate.total_equity, Decimal("1100"))
        self.assertEqual(candidate.order_reservations, (("42", Decimal("472")),))
        self.assertFalse(candidate.usable_for_risk)
        self.assertFalse(candidate.orders_enabled)

    def test_refuses_margin_borrow_isolation_liquidation_and_derivative_positions(self):
        with tempfile.TemporaryDirectory() as root:
            store, refs = btc_market(root)
            position = CryptoComPosition("BTCUSD-PERP", "PERPETUAL_SWAP", Decimal("1"), Decimal("60000"),
                Decimal("0"), Decimal("0"), NOW, "private/get-positions", "d" * 64)
            cases = (
                portfolio(borrow=Decimal("1")),
                portfolio(initial_margin=Decimal("1"), position_im=Decimal("1"), haircut=Decimal("0")),
                portfolio(isolated=Decimal("1")), portfolio(liquidating=True),
                portfolio(isolated_positions=True), portfolio(positions=(position,)),
            )
            for snapshot in cases:
                with self.subTest(snapshot=snapshot), self.assertRaises(CryptoComCandidateError):
                    evaluate_spot_portfolio_candidate(snapshot, refs, store, base_currency="USD", as_of=NOW)

    def test_accepts_reconciled_collateral_haircut_without_position_margin(self):
        with tempfile.TemporaryDirectory() as root:
            store, refs = btc_market(root)
            candidate = evaluate_spot_portfolio_candidate(
                portfolio(initial_margin=Decimal("100"), position_im=Decimal("0"), haircut=Decimal("100")),
                refs, store, base_currency="USD", as_of=NOW,
            )
        self.assertEqual(candidate.total_equity, Decimal("1100"))

    def test_refuses_unclassified_or_inconsistent_initial_margin(self):
        with tempfile.TemporaryDirectory() as root:
            store, refs = btc_market(root)
            cases = (
                portfolio(initial_margin=Decimal("100")),
                portfolio(initial_margin=Decimal("100"), position_im=Decimal("0"), haircut=Decimal("99")),
            )
            for snapshot in cases:
                with self.subTest(snapshot=snapshot), self.assertRaises(CryptoComCandidateError):
                    evaluate_spot_portfolio_candidate(snapshot, refs, store, base_currency="USD", as_of=NOW)

    def test_refuses_missing_stale_or_tampered_market_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            store, refs = btc_market(root)
            current = portfolio()
            with self.assertRaisesRegex(CryptoComCandidateError, "No verified"):
                evaluate_spot_portfolio_candidate(current, {}, store, base_currency="USD", as_of=NOW)
            stale_refs = {"BTC_USD": btc_market(root, observed_at=NOW - timedelta(seconds=60))[1]["BTC_USD"]}
            with self.assertRaisesRegex(CryptoComCandidateError, "STALE|stale"):
                evaluate_spot_portfolio_candidate(current, stale_refs, store, base_currency="USD", as_of=NOW)
            with self.assertRaises(CryptoComCandidateError):
                evaluate_spot_portfolio_candidate(current, {"BTC_USD": "f" * 64}, store,
                    base_currency="USD", as_of=NOW)

    def test_refuses_nonlimit_order_and_sell_quantity_outside_wallet(self):
        market_store = None
        with tempfile.TemporaryDirectory() as root:
            market_store, refs = btc_market(root)
            market_order = CryptoComOpenOrder("account", "43", None, "BTC_USD", "BUY", "MARKET", "ACTIVE",
                Decimal("0.01"), Decimal("0"), None, NOW, NOW, "private/get-open-orders", "e" * 64)
            with self.assertRaisesRegex(CryptoComCandidateError, "LIMIT"):
                evaluate_spot_portfolio_candidate(portfolio(open_orders=(market_order,)), refs, market_store,
                    base_currency="USD", as_of=NOW)
            sell_order = replace(market_order, order_id="44", side="SELL", order_type="LIMIT",
                limit_price=Decimal("61000"), quantity=Decimal("0.02"))
            with self.assertRaisesRegex(CryptoComCandidateError, "Combined open SELL"):
                evaluate_spot_portfolio_candidate(portfolio(open_orders=(sell_order,)), refs, market_store,
                    base_currency="USD", as_of=NOW)
            sell_order_two = replace(sell_order, order_id="45", quantity=Decimal("0.008"))
            sell_order_one = replace(sell_order, order_id="46", quantity=Decimal("0.008"))
            with self.assertRaisesRegex(CryptoComCandidateError, "Combined open SELL"):
                evaluate_spot_portfolio_candidate(portfolio(open_orders=(sell_order_one, sell_order_two)),
                    refs, market_store, base_currency="USD", as_of=NOW)


if __name__ == "__main__":
    unittest.main()
