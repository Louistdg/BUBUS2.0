from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bubus.research_data import (
    HistoricalDataError,
    archive_candle_dataset,
    fetch_candle_history,
)


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, amount: int = -1) -> bytes:
        return self.body if amount < 0 else self.body[:amount]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _PagedOpener:
    def __init__(self, *, wrong_symbol: bool = False, gap_at: int | None = None,
                 available_from: int | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.wrong_symbol = wrong_symbol
        self.gap_at = gap_at
        self.available_from = available_from

    def __call__(self, request, timeout: int, **_kwargs: object):
        self.assert_timeout = timeout
        query = parse_qs(urlsplit(request.full_url).query)
        start, end, count = int(query["start_ts"][0]), int(query["end_ts"][0]), int(query["count"][0])
        self.calls.append((start, end))
        rows = [
            {"t": stamp, "o": "100", "h": "101", "l": "99", "c": "100", "v": "10"}
            for stamp in range(start, end, 3_600_000)
            if stamp != self.gap_at and (self.available_from is None or stamp >= self.available_from)
        ][-count:]
        payload = {"code": 0, "result": {"instrument_name": "ETH_USD" if self.wrong_symbol else "BTC_USD",
                                             "data": rows}}
        return _Response(json.dumps(payload, separators=(",", ":")).encode())


class HistoricalResearchDataTests(unittest.TestCase):
    def test_fetches_all_pages_backwards_and_returns_chronological_coverage(self) -> None:
        opener = _PagedOpener()
        start, end = 3_600_000, 10 * 3_600_000

        dataset = fetch_candle_history("BTC_USD", "1h", start, end,
                                       page_size=3, opener=opener, now_ms=end)

        self.assertEqual(len(opener.calls), 3)
        self.assertEqual([row["timestamp"] for row in dataset.candles],
                         list(range(start, end, 3_600_000)))
        self.assertTrue(dataset.complete)
        self.assertEqual(dataset.missing_bars, 0)
        self.assertEqual(len(dataset.pages), 3)
        self.assertEqual(opener.calls[0][1], end)
        self.assertEqual(opener.calls[1][1], 7 * 3_600_000)

    def test_archive_keeps_raw_pages_and_manifest_hashes_without_overwrite(self) -> None:
        opener = _PagedOpener()
        dataset = fetch_candle_history("BTC_USD", "1h", 3_600_000, 6 * 3_600_000,
                                       page_size=2, opener=opener, now_ms=6 * 3_600_000)
        with tempfile.TemporaryDirectory() as directory:
            archive = archive_candle_dataset(dataset, Path(directory))
            manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
            for page in manifest["pages"]:
                raw = (archive / page["raw_file"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), page["raw_sha256"])
            with self.assertRaises(FileExistsError):
                archive_candle_dataset(dataset, Path(directory))

    def test_rejects_wrong_instrument_and_malformed_market_rows(self) -> None:
        with self.assertRaises(HistoricalDataError):
            fetch_candle_history("BTC_USD", "1h", 3_600_000, 6 * 3_600_000,
                                 page_size=2, opener=_PagedOpener(wrong_symbol=True),
                                 now_ms=6 * 3_600_000)

    def test_rejects_unaligned_or_future_ranges(self) -> None:
        with self.assertRaises(HistoricalDataError):
            fetch_candle_history("BTC_USD", "1h", 1, 6 * 3_600_000,
                                 opener=_PagedOpener(), now_ms=6 * 3_600_000)
        with self.assertRaises(HistoricalDataError):
            fetch_candle_history("BTC_USD", "1h", 0, 7 * 3_600_000,
                                 opener=_PagedOpener(), now_ms=6 * 3_600_000)

    def test_gaps_are_reported_and_never_filled_with_synthetic_rows(self) -> None:
        start, end = 3_600_000, 10 * 3_600_000

        dataset = fetch_candle_history("BTC_USD", "1h", start, end,
                                       page_size=3, opener=_PagedOpener(gap_at=5 * 3_600_000),
                                       now_ms=end)

        self.assertFalse(dataset.complete)
        self.assertEqual(dataset.missing_bars, 1)
        self.assertEqual(len(dataset.candles), dataset.expected_bars - 1)

    def test_unavailable_older_history_returns_an_incomplete_dataset(self) -> None:
        start, end = 3_600_000, 10 * 3_600_000

        dataset = fetch_candle_history("BTC_USD", "1h", start, end,
                                       page_size=3, opener=_PagedOpener(available_from=4 * 3_600_000),
                                       now_ms=end)

        self.assertFalse(dataset.complete)
        self.assertEqual(dataset.candles[0]["timestamp"], 4 * 3_600_000)
        self.assertEqual(dataset.missing_bars, 3)

    def test_manifest_checksum_covers_the_exact_manifest_bytes(self) -> None:
        dataset = fetch_candle_history("BTC_USD", "1h", 3_600_000, 6 * 3_600_000,
                                       page_size=2, opener=_PagedOpener(), now_ms=6 * 3_600_000)
        with tempfile.TemporaryDirectory() as directory:
            archive = archive_candle_dataset(dataset, Path(directory))
            body = (archive / "manifest.json").read_bytes()
            self.assertEqual((archive / "manifest.sha256").read_text(encoding="ascii").strip(),
                             hashlib.sha256(body).hexdigest())


if __name__ == "__main__":
    unittest.main()
