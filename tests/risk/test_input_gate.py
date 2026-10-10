from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from invest_ai.risk import AssetType, GateStatus, Side, TradeProposal, parse_trade_proposal

NOW = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


def payload() -> dict[str, object]:
    return {
        "proposal_id": "proposal-1",
        "idempotency_key": "key-1",
        "instrument_id": "instrument-aapl",
        "symbol": "AAPL",
        "asset_type": "EQUITY",
        "side": "BUY",
        "quantity": "2.5",
        "strategy_id": "strategy-1",
        "created_at": "2026-10-07T09:59:00Z",
        "market_snapshot_id": "market-1",
        "portfolio_snapshot_id": "portfolio-1",
        "order_type": "LIMIT",
        "limit_price": "100.25",
        "stop_price": "90",
        "evidence_refs": ["evidence-1"],
    }


def parse(value: object) -> object:
    raw = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return parse_trade_proposal(raw, now=NOW)


class RiskInputGateTests(unittest.TestCase):
    def test_valid_json_becomes_typed_proposal(self):
        result = parse(payload())
        self.assertEqual(result.status, GateStatus.VALID)
        self.assertIsInstance(result.proposal, TradeProposal)
        self.assertEqual(result.proposal.asset_type, AssetType.EQUITY)
        self.assertEqual(result.proposal.side, Side.BUY)
        self.assertEqual(str(result.proposal.quantity), "2.5")
        self.assertEqual(result.proposal.evidence_refs, ("evidence-1",))

    def test_defaults_for_documented_optional_fields(self):
        data = payload()
        for key in ("order_type", "limit_price", "stop_price", "evidence_refs"):
            data.pop(key)
        result = parse(data)
        self.assertEqual(result.status, GateStatus.VALID)
        self.assertEqual(result.proposal.order_type, "MARKET")
        self.assertIsNone(result.proposal.limit_price)
        self.assertEqual(result.proposal.evidence_refs, ())

    def test_unknown_and_missing_fields_rejected(self):
        data = payload()
        data["is_approved"] = True
        self.assertIn("UNKNOWN_FIELDS", parse(data).reason_codes)
        data = payload()
        data.pop("strategy_id")
        self.assertIn("MISSING_FIELDS", parse(data).reason_codes)

    def test_duplicate_json_object_key_rejected(self):
        raw = json.dumps(payload(), separators=(",", ":"))[:-1] + ',"side":"SELL"}'
        self.assertIn("DUPLICATE_JSON_KEY", parse(raw).reason_codes)

    def test_json_float_and_nonstandard_nonfinite_numbers_rejected(self):
        raw = json.dumps(payload()).replace('"quantity": "2.5"', '"quantity": 2.5')
        self.assertIn("JSON_FLOAT_FORBIDDEN", parse(raw).reason_codes)
        raw = json.dumps(payload()).replace('"quantity": "2.5"', '"quantity": NaN')
        self.assertIn("NON_FINITE_JSON_NUMBER", parse(raw).reason_codes)

    def test_financial_values_must_be_finite_bounded_decimal_strings(self):
        for value in (2, "NaN", "Infinity", "1e51", "1" * 100, " 2", "1_0"):
            with self.subTest(value=value):
                data = payload()
                data["quantity"] = value
                self.assertIn("QUANTITY_FORMAT_INVALID", parse(data).reason_codes)

    def test_timestamps_must_be_aware_parseable_and_not_future(self):
        for stamp, expected in (
            ("2026-10-07T09:59:00", "TIMESTAMP_INVALID_OR_NAIVE"),
            ("not-a-time", "TIMESTAMP_INVALID_OR_NAIVE"),
            ("2026-10-07T10:00:06Z", "TIMESTAMP_FUTURE"),
        ):
            with self.subTest(stamp=stamp):
                data = payload()
                data["created_at"] = stamp
                self.assertIn(expected, parse(data).reason_codes)

    def test_five_second_created_at_skew_is_accepted_as_clock_tolerance(self):
        data = payload()
        data["created_at"] = (NOW + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
        self.assertEqual(parse(data).status, GateStatus.VALID)

    def test_nonpositive_quantity_is_rejected(self):
        data = payload()
        data["quantity"] = "0"
        self.assertIn("QUANTITY_NOT_POSITIVE", parse(data).reason_codes)

    def test_invalid_enums_and_unsupported_order_are_distinguished(self):
        data = payload()
        data["side"] = "SHORT"
        self.assertIn("SIDE_INVALID", parse(data).reason_codes)
        data = payload()
        data["asset_type"] = "TOKEN"
        self.assertIn("ASSET_TYPE_INVALID", parse(data).reason_codes)
        data = payload()
        data["order_type"] = "STOP_LIMIT"
        result = parse(data)
        self.assertEqual(result.status, GateStatus.UNSUPPORTED)
        self.assertIn("ORDER_TYPE_UNSUPPORTED", result.reason_codes)
        self.assertIsNone(result.proposal)

    def test_order_type_and_limit_price_contract_is_consistent(self):
        data = payload()
        data["order_type"] = "LIMIT"
        data.pop("limit_price")
        self.assertIn("LIMIT_PRICE_REQUIRED", parse(data).reason_codes)
        data = payload()
        data["order_type"] = "MARKET"
        self.assertIn("LIMIT_PRICE_NOT_ALLOWED", parse(data).reason_codes)

    def test_symbol_and_identifier_limits(self):
        for key, value, reason in (
            ("symbol", "aapl", "SYMBOL_INVALID"),
            ("symbol", "X" * 33, "SYMBOL_INVALID"),
            ("proposal_id", "bad id", "IDENTIFIER_INVALID"),
            ("instrument_id", "x" * 129, "IDENTIFIER_INVALID"),
        ):
            with self.subTest(key=key, value=value):
                data = payload()
                data[key] = value
                self.assertIn(reason, parse(data).reason_codes)

    def test_duplicate_and_malformed_evidence_are_rejected(self):
        for refs, reason in ((["e-1", "e-1"], "EVIDENCE_DUPLICATE"), (["bad id"], "EVIDENCE_FORMAT_INVALID"), ("e-1", "EVIDENCE_FORMAT_INVALID")):
            with self.subTest(refs=refs):
                data = payload()
                data["evidence_refs"] = refs
                self.assertIn(reason, parse(data).reason_codes)

    def test_oversized_input_is_rejected(self):
        self.assertIn("INPUT_TOO_LARGE", parse(" " * 65_537).reason_codes)

    def test_invalid_validation_clock_fails_closed(self):
        result = parse_trade_proposal(json.dumps(payload()), now=NOW.replace(tzinfo=None))
        self.assertEqual(result.status, GateStatus.INVALID)
        self.assertIn("VALIDATION_TIME_INVALID", result.reason_codes)


if __name__ == "__main__":
    unittest.main()
