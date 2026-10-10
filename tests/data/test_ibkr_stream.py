from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from invest_ai.data.ibkr_stream import (
    IBKRMarketDataStatus,
    IBKRStreamContractError,
    build_subscription_message,
    build_unsubscription_message,
    parse_market_data_message,
)


RAW = b'{"88":"19","conidEx":"721095500","7059":"1","_updated":1724934591499,"server_id":"q3","6509":"R","topic":"smd+721095500","conid":721095500,"6119":"q3","84":"0.18","85":"1","31":"0.19","86":"0.21"}'
OBSERVED = datetime(2024, 8, 29, 12, 29, 51, 499000, tzinfo=timezone.utc)
RECEIVED = OBSERVED + timedelta(seconds=1)


class IBKRStreamParserTests(unittest.TestCase):
    def parse(self, payload: bytes | str | dict = RAW):
        return parse_market_data_message(payload, expected_conid=721095500, received_at=RECEIVED)

    def test_official_example_maps_to_explicit_realtime_update(self) -> None:
        update = self.parse(RAW)
        self.assertEqual(update.conid, 721095500)
        self.assertEqual(update.status, IBKRMarketDataStatus.REALTIME)
        self.assertEqual(update.observed_at, OBSERVED)
        self.assertEqual(update.received_at, RECEIVED)
        self.assertEqual(update.bid, Decimal("0.18"))
        self.assertEqual(update.ask, Decimal("0.21"))
        self.assertEqual(update.last, Decimal("0.19"))
        self.assertEqual(update.bid_size, Decimal("19"))
        self.assertEqual(update.ask_size, Decimal("1"))
        self.assertEqual(len(update.payload_sha256), 64)
        self.assertTrue(update.is_usable_realtime_quote(now=RECEIVED, max_age=timedelta(seconds=3)))

    def test_delayed_frozen_unsubscribed_incomplete_and_unknown_never_count_as_live(self) -> None:
        source = json.loads(RAW)
        cases = {
            "D": IBKRMarketDataStatus.DELAYED,
            "Z": IBKRMarketDataStatus.FROZEN,
            "Y": IBKRMarketDataStatus.FROZEN_DELAYED,
            "N": IBKRMarketDataStatus.NOT_SUBSCRIBED,
            "i": IBKRMarketDataStatus.INCOMPLETE,
            "v": IBKRMarketDataStatus.VDR_EXEMPT_TIME_UNKNOWN,
            "?": IBKRMarketDataStatus.UNKNOWN,
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                item = dict(source, **{"6509": code + "pB"})
                update = self.parse(item)
                self.assertEqual(update.status, expected)
                self.assertFalse(update.is_usable_realtime_quote(now=RECEIVED, max_age=timedelta(seconds=3)))

    def test_missing_status_and_one_sided_quote_are_not_eligible(self) -> None:
        source = json.loads(RAW)
        del source["6509"]
        self.assertEqual(self.parse(source).status, IBKRMarketDataStatus.UNKNOWN)
        source["6509"] = "RpB"
        del source["86"]
        update = self.parse(source)
        self.assertFalse(update.has_two_sided_quote)
        self.assertFalse(update.is_usable_realtime_quote(now=RECEIVED, max_age=timedelta(seconds=3)))

    def test_crossed_stale_and_future_updates_fail_quality_gate(self) -> None:
        source = json.loads(RAW)
        source["84"], source["86"] = "0.25", "0.21"
        crossed = self.parse(source)
        self.assertTrue(crossed.is_crossed)
        self.assertFalse(crossed.is_usable_realtime_quote(now=RECEIVED, max_age=timedelta(seconds=3)))

        regular = self.parse(RAW)
        self.assertFalse(regular.is_usable_realtime_quote(now=RECEIVED, max_age=timedelta(milliseconds=100)))
        with self.assertRaises(IBKRStreamContractError):
            regular.is_fresh(now=RECEIVED, max_age=timedelta(0))
        future_received = OBSERVED - timedelta(seconds=6)
        with self.assertRaises(IBKRStreamContractError):
            parse_market_data_message(RAW, expected_conid=721095500, received_at=future_received)

    def test_invalid_source_shapes_and_values_are_rejected(self) -> None:
        source = json.loads(RAW)
        for change in (
            {"conid": 1},
            {"topic": "smd+1"},
            {"_updated": 1724934591},
            {"84": 0.18},
            {"84": "NaN"},
            {"85": "-1"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(IBKRStreamContractError):
                    self.parse(dict(source, **change))
        with self.assertRaises(IBKRStreamContractError):
            parse_market_data_message(RAW, expected_conid=True, received_at=RECEIVED)
        with self.assertRaises(IBKRStreamContractError):
            parse_market_data_message(RAW, expected_conid=721095500, received_at=datetime(2024, 8, 29))
        with self.assertRaises(IBKRStreamContractError):
            self.parse("not-json")

    def test_subscription_frames_match_documented_stream_protocol(self) -> None:
        self.assertEqual(
            build_subscription_message(721095500),
            'smd+721095500+{"fields":["31","84","85","86","88","6509"]}',
        )
        self.assertEqual(build_unsubscription_message(721095500), "umd+721095500+{}")
        for invalid in (0, -1, True, "721095500"):
            with self.subTest(invalid=invalid), self.assertRaises(IBKRStreamContractError):
                build_subscription_message(invalid)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
