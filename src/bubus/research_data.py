"""Point-in-time Crypto.com candle collection for offline strategy research.

This module uses public market data only. It never reads credentials, account
balances, or trading interfaces. Raw response pages are retained with hashes.
"""
from __future__ import annotations

import hashlib
import json
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_URL = "https://api.crypto.com/exchange/v1/public/get-candlestick"
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,15}_[A-Z]{3,12}\Z")
_STEP_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000,
            "4h": 14_400_000, "1D": 86_400_000}
_MAX_PAGE_SIZE = 300
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class HistoricalDataError(ValueError):
    """Historical market data could not be safely validated or archived."""


@dataclass(frozen=True, slots=True)
class HistoricalCandlePage:
    page_number: int
    requested_start_ms: int
    requested_end_ms: int
    raw_body: bytes = field(repr=False)
    raw_sha256: str
    row_count: int
    first_timestamp_ms: int | None
    last_timestamp_ms: int | None


@dataclass(frozen=True, slots=True)
class HistoricalCandleDataset:
    symbol: str
    timeframe: str
    start_ms: int
    end_ms: int
    collected_at_utc: str
    candles: tuple[dict[str, object], ...]
    pages: tuple[HistoricalCandlePage, ...]
    expected_bars: int
    missing_bars: int
    complete: bool


def _parse_page(body: bytes, *, symbol: str, start_ms: int, end_ms: int, step_ms: int,
                max_rows: int, page_number: int,
                request_start_ms: int) -> tuple[list[dict[str, object]], HistoricalCandlePage]:
    try:
        payload = json.loads(body, parse_float=Decimal,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        result = payload["result"]
        if payload.get("code") != 0 or result.get("instrument_name") != symbol:
            raise ValueError
        rows = result["data"]
        if not isinstance(rows, list) or len(rows) > max_rows:
            raise ValueError
        normalized: list[dict[str, object]] = []
        for row in rows:
            stamp = row["t"]
            if type(stamp) is not int or not start_ms <= stamp < end_ms or stamp % step_ms:
                raise ValueError
            values = {key: Decimal(str(row[key])) for key in ("o", "h", "l", "c", "v")}
            if any(not value.is_finite() for value in values.values()):
                raise ValueError
            if (min(values[key] for key in ("o", "h", "l", "c")) <= 0 or values["v"] < 0
                    or values["h"] < max(values["o"], values["c"], values["l"])
                    or values["l"] > min(values["o"], values["c"])):
                raise ValueError
            normalized.append({"timestamp": stamp, **{key: str(values[key]) for key in values}})
        normalized.sort(key=lambda item: item["timestamp"])
        timestamps = [int(item["timestamp"]) for item in normalized]
        if len(set(timestamps)) != len(timestamps) or any(stamp >= request_start_ms for stamp in timestamps):
            raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError):
        raise HistoricalDataError("Réponse historique Crypto.com invalide ou incohérente.") from None
    page = HistoricalCandlePage(
        page_number=page_number,
        requested_start_ms=start_ms,
        requested_end_ms=request_start_ms,
        raw_body=body,
        raw_sha256=hashlib.sha256(body).hexdigest(),
        row_count=len(normalized),
        first_timestamp_ms=timestamps[0] if timestamps else None,
        last_timestamp_ms=timestamps[-1] if timestamps else None,
    )
    return normalized, page


def fetch_candle_history(
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    *,
    page_size: int = _MAX_PAGE_SIZE,
    max_pages: int = 20_000,
    opener: Callable[..., object] | None = None,
    now_ms: int | None = None,
) -> HistoricalCandleDataset:
    """Fetch a closed UTC candle range backwards in bounded, hashed pages.

    `end_ms` is exclusive. Missing bars are reported and never interpolated.
    Crypto.com currently returns at most 300 candles per request in observed
    public API responses; the collector enforces that page ceiling.
    """
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise HistoricalDataError("Paire Spot invalide.")
    if not isinstance(timeframe, str) or timeframe not in _STEP_MS:
        raise HistoricalDataError("Période de bougie non prise en charge.")
    step = _STEP_MS[timeframe]
    if (type(start_ms) is not int or type(end_ms) is not int or start_ms < 0
            or start_ms >= end_ms or start_ms % step or end_ms % step):
        raise HistoricalDataError("Les bornes doivent être alignées sur des bougies UTC fermées.")
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if type(current_ms) is not int or end_ms > current_ms:
        raise HistoricalDataError("La borne de fin ne peut pas inclure une bougie future ou encore ouverte.")
    if type(page_size) is not int or not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise HistoricalDataError("La taille de page doit être comprise entre 1 et 300.")
    if type(max_pages) is not int or max_pages < 1:
        raise HistoricalDataError("max_pages doit être un entier positif.")

    request_opener = opener or urlopen
    cursor = end_ms
    pages: list[HistoricalCandlePage] = []
    combined: dict[int, dict[str, object]] = {}
    for page_number in range(1, max_pages + 1):
        query = urlencode({"instrument_name": symbol, "timeframe": timeframe,
                           "count": page_size, "start_ts": start_ms, "end_ts": cursor})
        request = Request(f"{_URL}?{query}", headers={"Accept": "application/json",
                                                       "User-Agent": "BUBUS2.0-Research/1"})
        try:
            with request_opener(request, timeout=15, context=ssl.create_default_context()) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError
        except Exception:
            raise HistoricalDataError("La collecte publique historique Crypto.com a échoué.") from None
        rows, page = _parse_page(raw, symbol=symbol, start_ms=start_ms, end_ms=end_ms,
                                 step_ms=step, max_rows=page_size, page_number=page_number,
                                 request_start_ms=cursor)
        pages.append(page)
        if not rows:
            break
        for row in rows:
            stamp = int(row["timestamp"])
            if stamp in combined:
                raise HistoricalDataError("Des pages historiques se chevauchent; collecte arrêtée.")
            combined[stamp] = row
        first_stamp = page.first_timestamp_ms
        if first_stamp <= start_ms:
            break
        if first_stamp >= cursor:
            raise HistoricalDataError("La pagination historique n'avance pas.")
        cursor = first_stamp
    else:
        raise HistoricalDataError("La limite de pages est atteinte avant la borne de début.")

    candles = tuple(combined[stamp] for stamp in sorted(combined))
    expected = (end_ms - start_ms) // step
    observed_stamps = set(combined)
    missing = sum(1 for stamp in range(start_ms, end_ms, step) if stamp not in observed_stamps)
    complete = len(candles) == expected and missing == 0
    return HistoricalCandleDataset(
        symbol=symbol, timeframe=timeframe, start_ms=start_ms, end_ms=end_ms,
        collected_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        candles=candles, pages=tuple(pages), expected_bars=expected,
        missing_bars=missing, complete=complete,
    )


def archive_candle_dataset(dataset: HistoricalCandleDataset, root: Path) -> Path:
    """Create a new non-overwriting archive with raw pages and a manifest."""
    if not isinstance(dataset, HistoricalCandleDataset) or not isinstance(root, Path):
        raise TypeError("HistoricalCandleDataset and Path are required.")
    root.mkdir(parents=True, exist_ok=True)
    name = f"{dataset.symbol}_{dataset.timeframe}_{dataset.start_ms}_{dataset.end_ms}"
    destination = root / name
    destination.mkdir(exist_ok=False)
    page_manifest = []
    for page in dataset.pages:
        raw_name = f"page-{page.page_number:05d}.json"
        (destination / raw_name).write_bytes(page.raw_body)
        page_manifest.append({
            "page_number": page.page_number,
            "requested_start_ms": page.requested_start_ms,
            "requested_end_ms": page.requested_end_ms,
            "raw_file": raw_name,
            "raw_sha256": page.raw_sha256,
            "row_count": page.row_count,
            "first_timestamp_ms": page.first_timestamp_ms,
            "last_timestamp_ms": page.last_timestamp_ms,
        })
    candles_path = destination / "candles.jsonl"
    candle_bytes = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                            for row in dataset.candles).encode("utf-8")
    candles_path.write_bytes(candle_bytes)
    manifest = {
        "schema": "cryptocom-ohlcv-research-v1",
        "provider": "Crypto.com Exchange public API",
        "symbol": dataset.symbol,
        "timeframe": dataset.timeframe,
        "start_ms_inclusive": dataset.start_ms,
        "end_ms_exclusive": dataset.end_ms,
        "collected_at_utc": dataset.collected_at_utc,
        "expected_bars": dataset.expected_bars,
        "observed_bars": len(dataset.candles),
        "missing_bars": dataset.missing_bars,
        "complete": dataset.complete,
        "candles_file": "candles.jsonl",
        "candles_sha256": hashlib.sha256(candle_bytes).hexdigest(),
        "pages": page_manifest,
        "orders_enabled": False,
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    (destination / "manifest.json").write_bytes(manifest_bytes)
    (destination / "manifest.sha256").write_text(hashlib.sha256(manifest_bytes).hexdigest() + "\n", encoding="ascii")
    return destination


__all__ = ["HistoricalCandleDataset", "HistoricalCandlePage", "HistoricalDataError",
           "archive_candle_dataset", "fetch_candle_history"]
