from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from invest_ai.data.models import DataProvenance, Instrument, MarketBar, Quote
from invest_ai.monitoring import (
    MarketAnomalyMonitor,
    MarketMonitorConfig,
    MarketMonitorError,
)


BASE = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
INSTRUMENT = Instrument(
    "synthetic-eq-1", "SYNX", "EQUITY", "USD", exchange="XNAS", venue="XNAS",
    tick_size=Decimal("0.01"), price_convention="PER_SHARE",
)


def provenance(
    record_id: str,
    observed_at: datetime,
    *,
    dataset: str,
    provider: str = "SYNTHETIC_FIXTURE",
    unit: str = "shares",
    revision: int = 1,
    delay: timedelta = timedelta(seconds=1),
) -> DataProvenance:
    return DataProvenance(
        provider=provider,
        source="fixture://market-replay",
        dataset=dataset,
        instrument_id=INSTRUMENT.instrument_id,
        observed_at=observed_at,
        published_at=observed_at,
        received_at=observed_at + delay,
        available_at=observed_at + delay,
        revision=revision,
        currency="USD",
        unit=unit,
        raw_reference=f"fixtures/{record_id}.json",
        raw_sha256=hashlib.sha256(record_id.encode()).hexdigest(),
        record_id=record_id,
    )


def bar(index: int, price: str, volume: str, *, observed_at: datetime | None = None,
        adjusted: bool | None = False, record_id: str | None = None) -> MarketBar:
    close = Decimal(price)
    point = observed_at or (BASE + timedelta(seconds=index * 5))
    source = provenance(record_id or f"bar-{index}", point, dataset="market_bar")
    return MarketBar(
        provenance=source, interval="5s", bar_start=point - timedelta(seconds=5),
        bar_end=point, open=close, high=close, low=close, close=close,
        volume=Decimal(volume), adjusted=adjusted,
    )


def quote(index: int, bid: str, ask: str, bid_size: str | None = "100",
          ask_size: str | None = "100", *, record_id: str | None = None,
          observed_at: datetime | None = None) -> Quote:
    point = observed_at or (BASE + timedelta(seconds=index))
    source = provenance(record_id or f"quote-{index}", point, dataset="quote", unit="shares")
    return Quote(
        provenance=source, bid=Decimal(bid), ask=Decimal(ask),
        bid_size=Decimal(bid_size) if bid_size is not None else None,
        ask_size=Decimal(ask_size) if ask_size is not None else None,
        venue="XNAS",
    )


def ingest(monitor: MarketAnomalyMonitor, observation: MarketBar | Quote,
           *, delay: timedelta = timedelta(seconds=2)):
    return monitor.ingest(observation, INSTRUMENT,
                          as_of=observation.provenance.available_at + delay)


class MarketAnomalyMonitorTests(unittest.TestCase):
    def test_price_jump_and_volume_spike_are_evidence_linked_review_alerts(self) -> None:
        monitor = MarketAnomalyMonitor(MarketMonitorConfig(lookback=3))
        for index in range(3):
            self.assertEqual(ingest(monitor, bar(index, "100", "100")).alerts, ())

        result = ingest(monitor, bar(3, "105", "350"))
        self.assertEqual({alert.rule_code for alert in result.alerts}, {"PRICE_MOVE", "VOLUME_SPIKE"})
        price_alert = next(alert for alert in result.alerts if alert.rule_code == "PRICE_MOVE")
        volume_alert = next(alert for alert in result.alerts if alert.rule_code == "VOLUME_SPIKE")
        self.assertEqual(price_alert.direction, "UP")
        self.assertEqual(price_alert.observed_value, Decimal("5"))
        self.assertEqual(price_alert.observation_id, result.observation_id)
        self.assertEqual(volume_alert.baseline_value, Decimal("100"))
        self.assertEqual(volume_alert.threshold_value, Decimal("300"))
        self.assertEqual(result.authority_status, "OBSERVATION_ONLY")
        self.assertIsNone(result.trade_proposal)

    def test_replay_is_deterministic_for_same_point_in_time_inputs(self) -> None:
        stream = [bar(index, price, volume) for index, (price, volume) in enumerate(
            (("100", "100"), ("100.5", "110"), ("101", "100"), ("96", "400")))]
        results = []
        for _ in range(2):
            monitor = MarketAnomalyMonitor(MarketMonitorConfig(lookback=3))
            results.append(tuple(ingest(monitor, item) for item in stream))
        self.assertEqual(results[0], results[1])

    def test_duplicate_observation_is_idempotent_and_conflicting_replay_is_rejected(self) -> None:
        monitor = MarketAnomalyMonitor()
        observation = bar(0, "100", "100")
        first = ingest(monitor, observation)
        duplicate = ingest(monitor, observation)
        self.assertIs(first, duplicate)
        changed = replace(observation, close=Decimal("101"), high=Decimal("101"))
        with self.assertRaisesRegex(MarketMonitorError, "different content"):
            ingest(monitor, changed)
        with self.assertRaisesRegex(MarketMonitorError, "different content"):
            monitor.ingest(observation, replace(INSTRUMENT, symbol="OTHER"),
                           as_of=observation.provenance.available_at + timedelta(seconds=2))

    def test_out_of_order_observations_and_adjusted_bars_fail_closed(self) -> None:
        monitor = MarketAnomalyMonitor()
        first = bar(2, "100", "100")
        ingest(monitor, first)
        earlier = bar(1, "101", "100")
        with self.assertRaisesRegex(MarketMonitorError, "out-of-order"):
            ingest(monitor, earlier)
        with self.assertRaisesRegex(MarketMonitorError, "adjusted or unknown"):
            ingest(monitor, bar(3, "100", "100", adjusted=None))
        with self.assertRaisesRegex(MarketMonitorError, "adjusted or unknown"):
            ingest(monitor, bar(4, "100", "100", adjusted=True))

    def test_missing_bar_volume_and_future_observations_are_rejected(self) -> None:
        monitor = MarketAnomalyMonitor()
        source = provenance("no-volume", BASE, dataset="market_bar")
        incomplete = MarketBar(
            provenance=source, interval="5s", bar_start=BASE - timedelta(seconds=5),
            bar_end=BASE, open="100", high="100", low="100", close="100", volume=None,
            adjusted=False,
        )
        with self.assertRaisesRegex(MarketMonitorError, "requires open, high, low, close, and volume"):
            ingest(monitor, incomplete)
        future = bar(0, "100", "100", observed_at=BASE + timedelta(minutes=1))
        with self.assertRaisesRegex(MarketMonitorError, "future"):
            monitor.ingest(future, INSTRUMENT, as_of=BASE)

    def test_stale_observation_is_reported_but_does_not_poison_price_baseline(self) -> None:
        monitor = MarketAnomalyMonitor(MarketMonitorConfig(
            price_move_percent=Decimal("3"), lookback=3,
            max_observation_age=timedelta(seconds=10),
        ))
        ingest(monitor, bar(0, "100", "100"))
        stale_point = BASE + timedelta(seconds=6)
        stale = bar(1, "1", "100", observed_at=stale_point)
        result = monitor.ingest(stale, INSTRUMENT,
                                as_of=stale_point + timedelta(seconds=30))
        self.assertTrue(result.stale)
        self.assertEqual([item.rule_code for item in result.alerts], ["STALE_OBSERVATION"])
        fresh = bar(2, "101", "100", observed_at=BASE + timedelta(seconds=12))
        fresh_result = ingest(monitor, fresh)
        self.assertFalse(any(item.rule_code == "PRICE_MOVE" for item in fresh_result.alerts))

    def test_quote_spread_and_top_of_book_imbalance_are_explicitly_scoped(self) -> None:
        monitor = MarketAnomalyMonitor(MarketMonitorConfig(lookback=3))
        for index in range(3):
            self.assertEqual(ingest(monitor, quote(index, "99.99", "100.01")).alerts, ())
        result = ingest(monitor, quote(3, "99", "101", "900", "100"))
        self.assertEqual(result.book_scope, "TOP_OF_BOOK")
        self.assertEqual(result.top_book_imbalance, Decimal("0.8"))
        codes = {alert.rule_code for alert in result.alerts}
        self.assertEqual(codes, {"SPREAD_WIDENED", "TOP_OF_BOOK_IMBALANCE"})
        imbalance = next(alert for alert in result.alerts if alert.rule_code == "TOP_OF_BOOK_IMBALANCE")
        self.assertEqual(imbalance.direction, "BID_SIZE_HEAVIER")
        self.assertIn("ne représente pas le carnet complet", imbalance.explanation)

    def test_quote_without_size_has_no_depth_claim_and_crossed_quote_is_rejected(self) -> None:
        monitor = MarketAnomalyMonitor()
        result = ingest(monitor, quote(0, "99.99", "100.01", None, None))
        self.assertEqual(result.book_scope, "QUOTE_ONLY")
        self.assertIsNone(result.top_book_imbalance)
        with self.assertRaisesRegex(MarketMonitorError, "crossed quote"):
            ingest(monitor, quote(1, "100.02", "100.01"))

    def test_provider_and_interval_streams_do_not_share_rolling_baselines(self) -> None:
        monitor = MarketAnomalyMonitor()
        first = bar(0, "100", "100")
        ingest(monitor, first)
        other_source = replace(first, provenance=replace(first.provenance, provider="OTHER_FIXTURE"),
                               close=Decimal("110"), high=Decimal("110"))
        result = ingest(monitor, other_source)
        self.assertFalse(any(alert.rule_code == "PRICE_MOVE" for alert in result.alerts))


if __name__ == "__main__":
    unittest.main()
