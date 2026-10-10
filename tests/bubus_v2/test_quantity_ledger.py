"""Synthetic exact asset quantity reconciliation; no exchange orders or secrets."""
import unittest
from bubus.quantity_ledger import AssetLedgerError, reconcile_asset_quantities, spot_trade_postings

SHA = "f" * 64


def posting(event_id, leg_id, kind, asset, delta, external_reference=None):
    return {"event_id": event_id, "leg_id": leg_id, "kind": kind,
            "asset": asset, "delta": delta, "source_sha256": SHA,
            "external_reference": external_reference}


def valid_entries():
    return [
        posting("deposit", "1", "DEPOSIT", "USDT", "100", "external-deposit"),
        posting("trade", "1", "TRADE", "USDT", "-200"),
        posting("trade", "2", "TRADE", "BTC", "1"),
        posting("fee", "1", "FEE", "USDT", "-2"),
        posting("conversion", "1", "CONVERSION", "USDT", "-50"),
        posting("conversion", "2", "CONVERSION", "EUR", "45"),
        posting("withdrawal", "1", "WITHDRAWAL", "EUR", "-5", "external-withdrawal"),
    ]


class QuantityLedgerTests(unittest.TestCase):
    def setUp(self):
        self.opening = {"USDT": "1000", "BTC": "0", "EUR": "0"}
        self.closing = {"USDT": "848", "BTC": "1", "EUR": "40"}

    def test_exact_arithmetic_match_never_grants_financial_authority(self):
        result = reconcile_asset_quantities(
            opening=self.opening, closing=self.closing, postings=valid_entries())
        self.assertEqual(result["status"], "MATCHED_ARITHMETIC_ONLY")
        self.assertEqual(result["posting_count"], 7)
        self.assertEqual(result["residual_quantities"], {})
        self.assertEqual(result["reasons"], [])
        self.assertFalse(result["provider_history_completeness_verified"])
        self.assertIsNone(result["pnl"])
        self.assertFalse(result["usable_for_risk"])
        self.assertFalse(result["usable_for_execution"])
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(result, reconcile_asset_quantities(
            opening=self.opening, closing=self.closing, postings=valid_entries()))

    def test_missing_sources_or_conversion_leg_block(self):
        absent = reconcile_asset_quantities(
            opening=None, closing=self.closing, postings=valid_entries())
        self.assertEqual(absent["status"], "BLOCKED_INCOMPLETE")
        self.assertIn("OPENING_OR_CLOSING_MISSING", absent["reasons"])
        without_postings = reconcile_asset_quantities(
            opening=self.opening, closing=self.closing, postings=None)
        self.assertIn("POSTINGS_MISSING", without_postings["reasons"])
        without_asset = dict(self.closing)
        without_asset.pop("BTC")
        gap = reconcile_asset_quantities(
            opening=self.opening, closing=without_asset, postings=valid_entries())
        self.assertIn("ASSET_OPENING_OR_CLOSING_MISSING", gap["reasons"])
        incomplete = valid_entries()[:-2] + valid_entries()[-1:]
        partial = reconcile_asset_quantities(
            opening=self.opening, closing=self.closing, postings=incomplete)
        self.assertIn("CONVERSION_OR_TRADE_LEG_MISSING", partial["reasons"])

    def test_conflicting_closing_balance_fails_without_inventing_value(self):
        contradiction = reconcile_asset_quantities(
            opening=self.opening, closing=dict(self.closing, USDT="999"),
            postings=valid_entries())
        self.assertEqual(contradiction["status"], "BLOCKED_CONTRADICTORY")
        self.assertEqual(contradiction["residual_quantities"]["USDT"], "-151")
        self.assertIn("QUANTITY_MISMATCH", contradiction["reasons"])
        self.assertIsNone(contradiction["pnl"])

    def test_invalid_amount_duplicate_unverified_fee_or_flow_fail_closed(self):
        variants = [
            valid_entries() + [valid_entries()[0]],
            [dict(valid_entries()[0], delta="NaN")],
            [dict(valid_entries()[0], delta="Infinity")],
            [dict(valid_entries()[0], delta="-100")],
            [dict(valid_entries()[3], delta="2")],
            [dict(valid_entries()[0], source_sha256="not-a-hash")],
            [dict(valid_entries()[0], asset="")],
            [dict(valid_entries()[1], external_reference="unwanted")],
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(AssetLedgerError):
                    reconcile_asset_quantities(
                        opening=self.opening, closing=self.closing, postings=variant)

    def test_spot_trade_projection_creates_signed_legs_and_explicit_fee_currency(self):
        trade = {"trade_id": "123", "instrument_name": "BTC_USDT", "side": "BUY",
                 "quantity": "0.002", "price": "50000", "fees": "-0.05",
                 "fee_currency": "CRO", "source": "private/get-trades", "payload_sha256": SHA}
        entries = spot_trade_postings([trade])
        self.assertEqual([(row["asset"], row["delta"], row["kind"]) for row in entries], [
            ("BTC", "0.002", "TRADE"), ("USDT", "-100.000", "TRADE"), ("CRO", "-0.05", "FEE")])
        result = reconcile_asset_quantities(
            opening={"BTC": "0", "USDT": "100", "CRO": "1"},
            closing={"BTC": "0.002", "USDT": "0", "CRO": "0.95"}, postings=entries)
        self.assertEqual(result["status"], "MATCHED_ARITHMETIC_ONLY")
        self.assertIsNone(result["pnl"])
        self.assertFalse(result["usable_for_risk"])
        self.assertFalse(result["orders_enabled"])

    def test_sell_projection_and_invalid_spot_trade_are_fail_closed(self):
        trade = {"trade_id": "124", "instrument_name": "ETH_EUR", "side": "SELL",
                 "quantity": "0.1", "price": "2000", "fees": "0",
                 "fee_currency": "EUR", "source": "private/get-trades", "payload_sha256": SHA}
        entries = spot_trade_postings([trade])
        self.assertEqual([(row["asset"], row["delta"]) for row in entries], [
            ("ETH", "-0.1"), ("EUR", "200.0")])
        for invalid in (
            dict(trade, instrument_name="BTCUSD-PERP"),
            dict(trade, fees="0.01"),
            dict(trade, source="fixture"),
            dict(trade, fee_currency=""),
            dict(trade, side=[]),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AssetLedgerError):
                    spot_trade_postings([invalid])
        with self.assertRaises(AssetLedgerError):
            spot_trade_postings([trade, trade])


if __name__ == "__main__":
    unittest.main()
