from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from invest_ai.data.archive import ArchiveError, NormalizedArchive, RawArchive
from invest_ai.data.models import (
    DataContractError, DataProvenance, Filing, Instrument, MacroObservation,
    MarketBar, NewsItem, ProviderMetadata, Quote, FundamentalFact,
)
from invest_ai.data.normalization import normalize_market_bar
from invest_ai.data.market import MarketSnapshot, build_market_snapshot
from invest_ai.data.pit import is_stale, select_as_of
from invest_ai.data.pipeline import DataIngestor
from invest_ai.data.providers import OfflineFixtureProvider, ProviderRequest, RawPayload


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "tests" / "data" / "fixtures" / "daily_bar.json"


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def provenance(*, record_id: str = "bar-1", revision: int = 1,
               observed_at: str = "2026-10-06T20:00:00Z",
               published_at: str | None = "2026-10-06T20:00:01Z",
               received_at: str = "2026-10-06T20:00:02Z",
               available_at: str = "2026-10-06T20:00:02Z",
               currency: str = "usd",
               raw: bytes = b"{}") -> DataProvenance:
    return DataProvenance(
        provider="fixture", source="fixture://offline", dataset="market_bar",
        instrument_id="us-equity-a", observed_at=observed_at,
        published_at=published_at, received_at=received_at, available_at=available_at,
        revision=revision, currency=currency, unit="USD/share",
        raw_reference="data/raw/fixture/market_bar/test.json",
        raw_sha256=hashlib.sha256(raw).hexdigest(), record_id=record_id,
    )


def bar(p: DataProvenance, price: str = "10.25", *, close: str | None = None) -> MarketBar:
    end = p.observed_at
    start = end - timedelta(days=1)
    open_price = Decimal(price)
    close_price = Decimal(close) if close is not None else open_price
    return MarketBar(
        provenance=p, interval="1d", bar_start=start, bar_end=end,
        open=open_price, high=max(open_price, close_price),
        low=min(open_price, close_price), close=close_price, volume=Decimal("100"),
        adjusted=False,
    )


def raw_payload(*, revision: int = 1, body: bytes | None = None,
                received_at: str = "2026-10-06T20:00:02Z",
                available_at: str | None = None) -> RawPayload:
    payload_body = body if body is not None else FIXTURE.read_bytes()
    received = utc(received_at)
    available = utc(available_at) if available_at else received
    return RawPayload(
        provider="offline_fixture", dataset="market_bar",
        source="fixture://tests/daily_bar.json", record_id="us-equity-a-2026-10-06",
        body=payload_body, observed_at=utc("2026-10-06T20:00:00Z"),
        published_at=utc("2026-10-06T20:00:01Z"),
        received_at=received, available_at=available, revision=revision,
        instrument_id="us-equity-a", currency="USD", unit="USD/share",
        media_type="application/json",
    )


class DataContractTests(unittest.TestCase):
    def test_crypto_asset_codes_are_supported_as_provenance_and_instrument_currency(self) -> None:
        item = provenance(currency="usdt")
        instrument = Instrument("crypto.com:BTC_USDT", "BTC_USDT", "CRYPTO", "usdt",
            exchange="Crypto.com Exchange", venue="CRYPTOCOM", instrument_type="SPOT")
        self.assertEqual(item.currency, "USDT")
        self.assertEqual(instrument.currency, "USDT")

    def test_currency_or_asset_code_rejects_punctuation(self) -> None:
        with self.assertRaisesRegex(DataContractError, "currency or asset code"):
            provenance(currency="US-DT")

    def test_utc_offset_is_normalized_and_timestamps_keep_distinct_meanings(self) -> None:
        item = provenance(
            observed_at="2026-10-06T22:00:00+02:00",
            published_at="2026-10-06T20:00:01Z",
            received_at="2026-10-06T20:00:02Z",
            available_at="2026-10-06T20:00:05Z",
        )
        self.assertEqual(item.observed_at, utc("2026-10-06T20:00:00Z"))
        self.assertEqual(item.published_at, utc("2026-10-06T20:00:01Z"))
        self.assertEqual(item.received_at, utc("2026-10-06T20:00:02Z"))
        self.assertEqual(item.available_at, utc("2026-10-06T20:00:05Z"))

    def test_naive_datetimes_are_rejected(self) -> None:
        with self.assertRaisesRegex(DataContractError, "timezone"):
            provenance(observed_at=datetime(2026, 10, 6, 20, 0))

    def test_available_at_cannot_precede_local_receipt(self) -> None:
        with self.assertRaisesRegex(DataContractError, "precede"):
            provenance(received_at="2026-10-06T20:00:02Z",
                       available_at="2026-10-06T20:00:01Z")

    def test_float_financial_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(DataContractError, "float"):
            MarketBar(
                provenance=provenance(), interval="1d",
                bar_start=utc("2026-10-05T20:00:00Z"),
                bar_end=utc("2026-10-06T20:00:00Z"),
                open=10.1, high=11, low=9, close=10, volume=1,
            )

    def test_missing_ohlc_is_explicit_and_not_usable(self) -> None:
        source = provenance()
        item = MarketBar(
            provenance=source, interval="1d",
            bar_start=source.observed_at - timedelta(days=1),
            bar_end=source.observed_at, open=10, high=11, low=9, close=None,
        )
        self.assertEqual(item.missing_fields, ("close",))
        self.assertFalse(item.is_usable)

    def test_invalid_ohlc_bounds_are_rejected(self) -> None:
        source = provenance()
        with self.assertRaisesRegex(DataContractError, "bounds"):
            MarketBar(
                provenance=source, interval="1d",
                bar_start=source.observed_at - timedelta(days=1),
                bar_end=source.observed_at, open=10, high=9, low=8, close=9,
            )

    def test_quote_missing_side_and_crossed_state_are_represented(self) -> None:
        source = provenance()
        one_sided = Quote(provenance=source, bid=10, ask=None)
        self.assertFalse(one_sided.is_complete)
        crossed = Quote(provenance=source, bid=11, ask=10)
        self.assertTrue(crossed.is_crossed)

    def test_quote_without_any_side_is_rejected(self) -> None:
        with self.assertRaisesRegex(DataContractError, "at least one"):
            Quote(provenance=provenance(), bid=None, ask=None)

    def test_missing_macro_value_requires_reason(self) -> None:
        with self.assertRaisesRegex(DataContractError, "missing_reason"):
            MacroObservation(provenance=provenance(), series_id="CPI",
                             period="2026-09", frequency="M", value=None)

    def test_instrument_and_other_required_models_validate(self) -> None:
        item = Instrument(instrument_id="i1", symbol="ABC", asset_type="EQUITY", currency="eur")
        self.assertEqual(item.currency, "EUR")
        source = provenance()
        self.assertEqual(FundamentalFact(source, "Revenue", "100.25").value, Decimal("100.25"))
        self.assertEqual(NewsItem(source, "Title", "Publisher", "https://example.test").title, "Title")
        self.assertEqual(Filing(source, "accession-1", "10-Q", source.observed_at,
                                "Quarterly report", "https://example.test/file").form, "10-Q")

    def test_provider_metadata_is_read_only(self) -> None:
        with self.assertRaisesRegex(DataContractError, "read-only"):
            ProviderMetadata("provider", "1", ("market_bar",), False, False, False)

    def test_raw_reference_rejects_windows_absolute_paths(self) -> None:
        with self.assertRaisesRegex(DataContractError, "safe relative path"):
            DataProvenance(
                provider="fixture", source="fixture", dataset="market_bar",
                instrument_id=None, observed_at=utc("2026-10-06T20:00:00Z"),
                published_at=None, received_at=utc("2026-10-06T20:00:01Z"),
                available_at=utc("2026-10-06T20:00:01Z"), revision=1,
                currency=None, unit=None, raw_reference="C:\\outside\\raw.json",
                raw_sha256=hashlib.sha256(b"{}").hexdigest(), record_id="r1",
            )


class PointInTimeTests(unittest.TestCase):
    def test_later_revision_is_hidden_until_its_available_time(self) -> None:
        first_p = provenance(
            revision=1, received_at="2026-10-06T20:00:02Z",
            available_at="2026-10-06T20:00:02Z", raw=b"revision-1",
        )
        corrected_p = provenance(
            revision=2, received_at="2026-10-06T22:00:00Z",
            available_at="2026-10-06T22:00:03Z", raw=b"revision-2",
        )
        first = bar(first_p, "10")
        corrected = bar(corrected_p, "10", close="10.5")
        before = select_as_of([first, corrected], "2026-10-06T21:00:00Z")
        after = select_as_of([first, corrected], "2026-10-06T23:00:00Z")
        self.assertEqual(len(before), 1)
        self.assertEqual(before[0].provenance.revision, 1)
        self.assertEqual(after[0].provenance.revision, 2)
        self.assertEqual(after[0].close, Decimal("10.5"))

    def test_published_received_and_available_are_not_collapsed(self) -> None:
        item = provenance(
            published_at="2026-10-06T20:00:00Z",
            received_at="2026-10-06T20:00:05Z",
            available_at="2026-10-06T20:00:10Z",
        )
        self.assertLess(item.published_at, item.received_at)
        self.assertLess(item.received_at, item.available_at)

    def test_future_information_is_excluded(self) -> None:
        future = bar(provenance(
            received_at="2026-10-07T10:00:00Z",
            available_at="2026-10-07T10:00:00Z",
        ))
        self.assertEqual(select_as_of([future], "2026-10-07T09:59:59Z"), [])

    def test_conflicting_payloads_with_same_revision_are_rejected(self) -> None:
        first = bar(provenance(revision=1, raw=b"first"), "10")
        conflict = bar(provenance(revision=1, raw=b"second"), "11")
        with self.assertRaisesRegex(DataContractError, "conflicting observations"):
            select_as_of([first, conflict], "2026-10-07T00:00:00Z")

    def test_stale_quotes_are_detected(self) -> None:
        item = Quote(provenance=provenance(
            observed_at="2026-10-06T20:00:00Z",
            received_at="2026-10-06T20:00:02Z",
            available_at="2026-10-06T20:00:02Z",
        ), bid=10, ask=11)
        self.assertFalse(is_stale(item, "2026-10-06T20:00:30Z", timedelta(minutes=1)))
        self.assertTrue(is_stale(item, "2026-10-06T20:02:00Z", timedelta(minutes=1)))
        with self.assertRaisesRegex(DataContractError, "negative"):
            is_stale(item, "2026-10-06T20:02:00Z", timedelta(seconds=-1))


class MarketSnapshotTests(unittest.TestCase):
    def make_instrument(self, **changes) -> Instrument:
        values = dict(
            instrument_id="us-equity-a", symbol="ABC", asset_type="EQUITY",
            currency="USD", exchange="XNYS", venue="XNYS", timezone="America/New_York",
            multiplier="100", tick_size="0.25", lot_size="1", calendar_id="XNYS",
            price_convention="USD_PER_SHARE",
        )
        values.update(changes)
        return Instrument(**values)

    def test_snapshot_preserves_provenance_timezone_terms_and_is_deterministic(self) -> None:
        item = bar(provenance())
        instrument = self.make_instrument()
        snapshot = build_market_snapshot(
            item, instrument, as_of="2026-10-06T21:00:00Z", max_age=timedelta(hours=2),
        )
        again = build_market_snapshot(
            item, instrument, as_of="2026-10-06T21:00:00Z", max_age=timedelta(hours=2),
        )
        self.assertEqual(snapshot, again)
        self.assertEqual(snapshot.snapshot_id, again.snapshot_id)
        self.assertEqual(snapshot.price, Decimal("10.25"))
        self.assertEqual(snapshot.currency, "USD")
        self.assertEqual(snapshot.venue, "XNYS")
        self.assertEqual(snapshot.timezone, "America/New_York")
        self.assertEqual(snapshot.multiplier, Decimal("100"))
        self.assertEqual(snapshot.tick_size, Decimal("0.25"))
        self.assertEqual(snapshot.lot_size, Decimal("1"))
        self.assertEqual(snapshot.calendar_id, "XNYS")
        self.assertEqual(snapshot.provider, "fixture")
        self.assertEqual(snapshot.raw_sha256, item.provenance.raw_sha256)
        self.assertEqual(snapshot.received_at, item.provenance.received_at)
        self.assertEqual(snapshot.available_at, item.provenance.available_at)
        self.assertFalse(snapshot.stale)
        self.assertFalse(snapshot.adjusted)

    def test_currency_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(DataContractError, "currency mismatch"):
            build_market_snapshot(
                bar(provenance()), self.make_instrument(currency="EUR"),
                as_of="2026-10-06T21:00:00Z", max_age=timedelta(hours=2),
            )

    def test_timezone_must_be_valid(self) -> None:
        with self.assertRaisesRegex(DataContractError, "IANA timezone"):
            self.make_instrument(timezone="NotATimezone")

    def test_tick_size_is_positive_and_bar_price_must_align(self) -> None:
        with self.assertRaisesRegex(DataContractError, "tick_size must be positive"):
            self.make_instrument(tick_size="0")
        with self.assertRaisesRegex(DataContractError, "tick size"):
            build_market_snapshot(
                bar(provenance(), "10.25"), self.make_instrument(tick_size="0.30"),
                as_of="2026-10-06T21:00:00Z", max_age=timedelta(hours=2),
            )

    def test_multiplier_must_be_positive_and_is_carried_to_snapshot(self) -> None:
        with self.assertRaisesRegex(DataContractError, "multiplier must be positive"):
            self.make_instrument(multiplier="0")
        snapshot = build_market_snapshot(
            bar(provenance()), self.make_instrument(multiplier="100"),
            as_of="2026-10-06T21:00:00Z", max_age=timedelta(hours=2),
        )
        self.assertEqual(snapshot.multiplier, Decimal("100"))

    def test_stale_data_is_flagged_without_hiding_provenance(self) -> None:
        snapshot = build_market_snapshot(
            bar(provenance()), self.make_instrument(),
            as_of="2026-10-07T00:00:00Z", max_age=timedelta(minutes=30),
        )
        self.assertTrue(snapshot.stale)

    def test_future_data_is_rejected_by_point_in_time_boundary(self) -> None:
        future = provenance(
            received_at="2026-10-06T22:00:00Z", available_at="2026-10-06T22:00:00Z",
        )
        with self.assertRaisesRegex(DataContractError, "future market data"):
            build_market_snapshot(
                bar(future), self.make_instrument(),
                as_of="2026-10-06T21:00:00Z", max_age=timedelta(hours=2),
            )

    def test_missing_venue_is_rejected(self) -> None:
        with self.assertRaisesRegex(DataContractError, "venue is required"):
            build_market_snapshot(
                bar(provenance()), self.make_instrument(venue=None, exchange=None),
                as_of="2026-10-06T21:00:00Z", max_age=timedelta(hours=2),
            )

    def test_quote_midpoint_snapshot_uses_quote_venue_and_instrument_units(self) -> None:
        quote = Quote(provenance=provenance(), bid="10.00", ask="10.50", venue="ARCX")
        snapshot = build_market_snapshot(
            quote, self.make_instrument(), as_of="2026-10-06T21:00:00Z",
            max_age=timedelta(hours=2),
        )
        self.assertEqual(snapshot.price, Decimal("10.25"))
        self.assertEqual(snapshot.venue, "ARCX")


class OfflineIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_fixture_adapter_archives_raw_before_normalization_and_links_hash(self) -> None:
        payload = raw_payload()
        metadata = ProviderMetadata(
            provider="offline_fixture", adapter_version="1",
            supported_datasets=("market_bar",), read_only=True,
            supports_realtime=False, supports_websocket=False,
            notes="Offline test fixture; no network access.",
        )
        adapter = OfflineFixtureProvider(metadata, [payload])
        ingestor = DataIngestor(str(self.project))

        def normalizer(raw: RawPayload, archived):
            raw_path = self.project.joinpath(*Path(archived.reference).parts)
            self.assertTrue(raw_path.is_file())
            self.assertEqual(raw_path.read_bytes(), raw.body)
            return normalize_market_bar(raw, archived)

        outcomes = ingestor.ingest(
            adapter, ProviderRequest(datasets=("market_bar",)),
            {"market_bar": normalizer},
        )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, "APPENDED")
        self.assertEqual(outcomes[0].raw_sha256, hashlib.sha256(payload.body).hexdigest())
        raw_file = self.project.joinpath(*Path(outcomes[0].raw_reference).parts)
        self.assertEqual(raw_file.read_bytes(), payload.body)
        normalized_path = self.project.joinpath(*Path(outcomes[0].normalized_reference).parts)
        stored = json.loads(normalized_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["provenance"]["raw_reference"], outcomes[0].raw_reference)
        self.assertEqual(stored["provenance"]["raw_sha256"], outcomes[0].raw_sha256)
        self.assertEqual(stored["close"], "103.75")

    def test_exact_duplicate_is_idempotent_and_revision_is_append_only(self) -> None:
        payload = raw_payload()
        metadata = ProviderMetadata(
            provider="offline_fixture", adapter_version="1",
            supported_datasets=("market_bar",), read_only=True,
            supports_realtime=False, supports_websocket=False,
        )
        adapter = OfflineFixtureProvider(metadata, [payload, payload])
        ingestor = DataIngestor(str(self.project))
        outcomes = ingestor.ingest(
            adapter, ProviderRequest(datasets=("market_bar",)),
            {"market_bar": normalize_market_bar},
        )
        self.assertEqual([item.status for item in outcomes], ["APPENDED", "DUPLICATE"])
        self.assertEqual(len(list((self.project / "data" / "normalized").rglob("*.json"))), 1)

        corrected_body = payload.body.replace(b'"close": "103.75"', b'"close": "103.80"')
        corrected = raw_payload(
            revision=2, body=corrected_body,
            received_at="2026-10-07T08:00:00Z",
            available_at="2026-10-07T08:00:02Z",
        )
        corrected_adapter = OfflineFixtureProvider(metadata, [corrected])
        updated = ingestor.ingest(
            corrected_adapter, ProviderRequest(datasets=("market_bar",)),
            {"market_bar": normalize_market_bar},
        )
        self.assertEqual(updated[0].status, "APPENDED")
        self.assertEqual(len(list((self.project / "data" / "normalized").rglob("*.json"))), 2)

    def test_raw_archive_is_content_addressed_and_byte_exact(self) -> None:
        payload = raw_payload()
        archive = RawArchive(self.project)
        first = archive.archive(payload)
        second = archive.archive(payload)
        self.assertEqual(first, second)
        target = self.project.joinpath(*Path(first.reference).parts)
        self.assertEqual(target.read_bytes(), payload.body)
        self.assertEqual(first.sha256, hashlib.sha256(payload.body).hexdigest())

    def test_normalized_store_refuses_missing_or_tampered_raw(self) -> None:
        payload = raw_payload()
        raw = RawArchive(self.project).archive(payload)
        observation = normalize_market_bar(payload, raw)
        raw_path = self.project.joinpath(*Path(raw.reference).parts)
        raw_path.write_bytes(b"tampered")
        with self.assertRaisesRegex(ArchiveError, "payload hash"):
            NormalizedArchive(self.project).append(observation)

    def test_invalid_path_components_are_rejected(self) -> None:
        payload = RawPayload(
            provider="../escape", dataset="market_bar", source="fixture",
            record_id="r1", body=b"{}", observed_at=utc("2026-10-06T20:00:00Z"),
            received_at=utc("2026-10-06T20:00:01Z"),
            available_at=utc("2026-10-06T20:00:01Z"),
        )
        with self.assertRaisesRegex(DataContractError, "provider/dataset"):
            RawArchive(self.project).archive(payload)

    def test_unrequested_or_unsupported_dataset_is_rejected(self) -> None:
        metadata = ProviderMetadata(
            provider="offline_fixture", adapter_version="1",
            supported_datasets=("market_bar",), read_only=True,
            supports_realtime=False, supports_websocket=False,
        )
        adapter = OfflineFixtureProvider(metadata, [raw_payload()])
        with self.assertRaisesRegex(DataContractError, "not supported"):
            list(adapter.fetch(ProviderRequest(datasets=("news",))))

    def test_dataset_cannot_be_normalized_into_a_different_model(self) -> None:
        payload = raw_payload()
        metadata = ProviderMetadata(
            provider="offline_fixture", adapter_version="1",
            supported_datasets=("market_bar",), read_only=True,
            supports_realtime=False, supports_websocket=False,
        )
        with self.assertRaisesRegex(DataContractError, "requires MarketBar"):
            DataIngestor(str(self.project)).ingest(
                OfflineFixtureProvider(metadata, [payload]),
                ProviderRequest(datasets=("market_bar",)),
                {"market_bar": lambda raw, archived: Quote(
                    provenance=normalize_market_bar(raw, archived).provenance,
                    bid="1", ask="2",
                )},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
