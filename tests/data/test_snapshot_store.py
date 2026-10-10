from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from invest_ai.data.archive import NormalizedArchive, RawArchive
from invest_ai.data.market import build_market_snapshot
from invest_ai.data.models import DataProvenance, Instrument, MarketBar, Quote, canonical_json_bytes
from invest_ai.data.models import DataContractError
from invest_ai.data.snapshot_store import MarketSnapshotStore, SnapshotStoreError


NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


class MarketSnapshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw_bytes = b'{"fixture":"synthetic"}'
        raw = RawArchive(self.root).archive(self._raw_payload())
        self.raw_reference = raw.reference
        self.provenance = DataProvenance(
            provider="offline", source="fixture://market", dataset="market_bar",
            instrument_id="instrument-1", observed_at=NOW - timedelta(minutes=2),
            published_at=NOW - timedelta(minutes=1), received_at=NOW - timedelta(seconds=50),
            available_at=NOW - timedelta(seconds=50), revision=1, currency="USD",
            unit="USD/share", raw_reference=raw.reference, raw_sha256=raw.sha256,
            record_id="bar-1",
        )
        self.instrument = Instrument("instrument-1", "XYZ", "EQUITY", "USD",
                                     exchange="XNAS", venue="XNAS", multiplier=Decimal("1"),
                                     price_convention="PER_SHARE")
        self.observation: MarketBar | Quote = MarketBar(
            provenance=self.provenance, interval="1d", bar_start=NOW - timedelta(days=1),
            bar_end=NOW - timedelta(minutes=2), open="10", high="11", low="9", close="10",
            volume="100", adjusted=False,
        )
        NormalizedArchive(self.root).append(self.observation)
        self.store = MarketSnapshotStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _raw_payload(self):
        from invest_ai.data.providers import RawPayload
        return RawPayload(
            provider="offline", dataset="market_bar", source="fixture://market", record_id="bar-1",
            body=self.raw_bytes, observed_at=NOW - timedelta(minutes=2),
            published_at=NOW - timedelta(minutes=1), received_at=NOW - timedelta(seconds=50),
            available_at=NOW - timedelta(seconds=50), instrument_id="instrument-1", currency="USD",
            unit="USD/share", media_type="application/json",
        )

    def _register(self):
        snapshot = build_market_snapshot(self.observation, self.instrument, as_of=NOW,
                                         max_age=timedelta(minutes=10))
        return self.store.register(snapshot, self.observation, self.instrument,
                                   max_age=timedelta(minutes=10))

    def test_register_readback_is_immutable_and_verifies_archive_hashes(self) -> None:
        record = self._register()
        loaded = self.store.get(record.snapshot_id)
        self.assertEqual(loaded.snapshot, record.snapshot)
        self.assertEqual(loaded.snapshot_sha256, record.snapshot_sha256)
        self.assertTrue(self.store.assess_for_research(record.snapshot_id, as_of=NOW).eligible)
        risk = self.store.assess_for_risk(record.snapshot_id, as_of=NOW, max_age=timedelta(minutes=10))
        self.assertFalse(risk.eligible)
        self.assertIn("NOT_A_QUOTE", risk.reasons)

    def test_replaced_raw_archive_is_refused(self) -> None:
        record = self._register()
        (self.root / record.snapshot.raw_reference).write_bytes(b"replaced")
        with self.assertRaisesRegex(SnapshotStoreError, "raw payload"):
            self.store.get(record.snapshot_id)

    def test_replaced_normalized_record_is_refused(self) -> None:
        record = self._register()
        normalized = self.root / record.normalized_reference
        normalized.write_bytes(b"{}")
        with self.assertRaisesRegex(SnapshotStoreError, "normalized source"):
            self.store.get(record.snapshot_id)

    def test_future_or_missing_snapshot_is_refused(self) -> None:
        with self.assertRaisesRegex(SnapshotStoreError, "unavailable"):
            self.store.get("0" * 64)
        with self.assertRaisesRegex(DataContractError, "future"):
            build_market_snapshot(self.observation, self.instrument,
                                  as_of=NOW - timedelta(days=1),
                                  max_age=timedelta(days=10))

    def test_fabricated_snapshot_content_is_refused(self) -> None:
        snapshot = build_market_snapshot(self.observation, self.instrument, as_of=NOW,
                                         max_age=timedelta(minutes=10))
        from dataclasses import replace
        with self.assertRaisesRegex(SnapshotStoreError, "does not match"):
            self.store.register(replace(snapshot, price=Decimal("99")), self.observation,
                                self.instrument, max_age=timedelta(minutes=10))

    def test_quote_can_be_risk_candidate_but_adjusted_bar_cannot(self) -> None:
        p = DataProvenance(
            provider="offline", source="fixture://quote", dataset="quote",
            instrument_id="instrument-1", observed_at=NOW - timedelta(seconds=5),
            published_at=NOW - timedelta(seconds=4), received_at=NOW - timedelta(seconds=3),
            available_at=NOW - timedelta(seconds=3), revision=1, currency="USD", unit="USD/share",
            raw_reference=self.provenance.raw_reference, raw_sha256=self.provenance.raw_sha256,
            record_id="quote-1",
        )
        quote = Quote(p, bid="9.99", ask="10.01", venue="XNAS")
        # Put the source under the normalized archive's content-addressed key.
        normalized_path = self.root / "data" / "normalized" / "offline" / "quote" / f"{p.observation_id}.json"
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        normalized_bytes = canonical_json_bytes(quote)
        normalized_path.write_bytes(normalized_bytes)
        snapshot = build_market_snapshot(quote, self.instrument, as_of=NOW,
                                         max_age=timedelta(minutes=1))
        record = self.store.register(snapshot, quote, self.instrument, max_age=timedelta(minutes=1))
        result = self.store.assess_for_risk(record.snapshot_id, as_of=NOW,
                                            max_age=timedelta(minutes=1))
        self.assertTrue(result.eligible)
        reloaded = MarketSnapshotStore(self.root).assess_for_risk(
            record.snapshot_id, as_of=NOW, max_age=timedelta(minutes=1))
        self.assertTrue(reloaded.eligible)

    def test_codex_d_attack_market_bar_mislabeled_as_quote_is_refused(self) -> None:
        """Reproduce the audit: actual daily MarketBar carries dataset='quote'."""
        from dataclasses import replace

        mislabeled_provenance = replace(self.provenance, dataset="quote")
        mislabeled_bar = replace(self.observation, provenance=mislabeled_provenance)
        NormalizedArchive(self.root).append(mislabeled_bar)
        snapshot = build_market_snapshot(mislabeled_bar, self.instrument, as_of=NOW,
                                         max_age=timedelta(minutes=10))
        record = self.store.register(snapshot, mislabeled_bar, self.instrument,
                                     max_age=timedelta(minutes=10))

        # Assess from a fresh store instance to prove the persisted type is re-read.
        assessment = MarketSnapshotStore(self.root).assess_for_risk(
            record.snapshot_id, as_of=NOW, max_age=timedelta(minutes=10))
        self.assertFalse(assessment.eligible)
        self.assertIn("OBSERVATION_NOT_QUOTE", assessment.reasons)

    def test_stale_snapshot_is_rejected_for_risk(self) -> None:
        record = self._register()
        assessment = self.store.assess_for_risk(record.snapshot_id, as_of=NOW,
                                                max_age=timedelta(seconds=30))
        self.assertFalse(assessment.eligible)
        self.assertIn("STALE", assessment.reasons)

    def test_adjusted_eod_is_never_a_risk_price_candidate(self) -> None:
        from dataclasses import replace
        adjusted = replace(self.observation, adjusted=True)
        normalized = self.root / "data" / "normalized" / "offline" / "market_bar" / f"{self.provenance.observation_id}.json"
        normalized.write_bytes(canonical_json_bytes(adjusted))
        snapshot = build_market_snapshot(adjusted, self.instrument, as_of=NOW,
                                         max_age=timedelta(minutes=10))
        record = self.store.register(snapshot, adjusted, self.instrument,
                                     max_age=timedelta(minutes=10))
        assessment = self.store.assess_for_risk(record.snapshot_id, as_of=NOW,
                                                max_age=timedelta(minutes=10))
        self.assertFalse(assessment.eligible)
        self.assertIn("ADJUSTED_PRICE", assessment.reasons)
        self.assertIn("NOT_A_QUOTE", assessment.reasons)


if __name__ == "__main__":
    unittest.main()
