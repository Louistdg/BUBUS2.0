"""Build observation-only Crypto.com breakout signals from current market evidence."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

from invest_ai.data.cryptocom_market_readonly import CryptoComMarketTick
from invest_ai.data.models import DataProvenance, Instrument, MarketBar, Quote
from invest_ai.strategy import BreakoutSignalObservation, evaluate_breakout_signal


_TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1D": 86400}
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,15}_[A-Z0-9]{2,12}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class LiveSignalError(ValueError):
    """Current Crypto.com market evidence cannot be evaluated as a signal."""


def observe_cryptocom_breakout(
    symbol: str,
    timeframe: str,
    candle_payload: Mapping[str, object],
    tick: CryptoComMarketTick,
    instrument_rules: Mapping[str, str],
    *,
    as_of: datetime | None = None,
) -> BreakoutSignalObservation:
    """Evaluate closed Crypto.com bars plus one fresh ticker quote; never sizes an order."""
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise LiveSignalError("La paire Spot est invalide.")
    if timeframe not in _TIMEFRAME_SECONDS:
        raise LiveSignalError("L’intervalle de bougie n’est pas pris en charge pour l’analyse live.")
    base_currency, quote_currency = symbol.split("_", 1)
    if (not isinstance(instrument_rules, Mapping)
            or instrument_rules.get("tradable") != "true"
            or instrument_rules.get("base_currency") != base_currency
            or instrument_rules.get("quote_currency") != quote_currency):
        raise LiveSignalError("Les règles Spot actuelles de cette paire sont indisponibles.")
    if (not isinstance(tick, CryptoComMarketTick) or tick.symbol != symbol
            or tick.exchange != "Crypto.com Exchange" or tick.quote_status != "COMPLETE"
            or tick.best_bid is None or tick.best_ask is None
            or tick.best_bid_size is None or tick.best_ask_size is None):
        raise LiveSignalError("Le bid/ask live complet de cette paire n’est pas disponible.")
    if (not isinstance(candle_payload, Mapping) or candle_payload.get("symbol") != symbol
            or candle_payload.get("timeframe") != timeframe
            or candle_payload.get("source") != "Crypto.com Exchange"):
        raise LiveSignalError("L’historique reçu ne correspond pas à la paire et à l’intervalle demandés.")
    digest = candle_payload.get("payload_sha256")
    received_value = candle_payload.get("received_at")
    rows = candle_payload.get("candles")
    if (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
            or not isinstance(received_value, (int, float)) or isinstance(received_value, bool)
            or not isinstance(rows, list)):
        raise LiveSignalError("La provenance de l’historique est invalide.")
    cutoff = as_of or datetime.now(timezone.utc)
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise LiveSignalError("L’heure d’évaluation doit inclure un fuseau horaire.")
    cutoff = cutoff.astimezone(timezone.utc)
    received_at = datetime.fromtimestamp(float(received_value), timezone.utc)
    if received_at > cutoff or (cutoff - received_at).total_seconds() > 30:
        raise LiveSignalError("L’historique de bougies est trop ancien.")
    duration = timedelta(seconds=_TIMEFRAME_SECONDS[timeframe])
    instrument_id = f"crypto.com:{symbol}"
    try:
        instrument = Instrument(
            instrument_id=instrument_id, symbol=symbol, asset_type="CRYPTO",
            currency=quote_currency, exchange="Crypto.com Exchange", venue="CRYPTOCOM",
            instrument_type="SPOT", tick_size=instrument_rules["price_tick_size"],
            lot_size=instrument_rules["quantity_tick_size"], price_convention="QUOTE_PER_BASE",
        )
        bars: list[MarketBar] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError
            timestamp = row.get("timestamp")
            if type(timestamp) is not int or timestamp <= 0 or timestamp in seen:
                raise ValueError
            seen.add(timestamp)
            start = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
            end = start + duration
            if end > cutoff:
                continue
            provenance = DataProvenance(
                provider="crypto_com_exchange",
                source="https://api.crypto.com/exchange/v1/public/get-candlestick",
                dataset="market_bar", instrument_id=instrument_id,
                observed_at=end, published_at=None, received_at=received_at,
                available_at=received_at, revision=1, currency=quote_currency,
                unit=f"{quote_currency}/{base_currency}",
                raw_reference=f"crypto.com/candles/{symbol}/{timeframe}/{timestamp}.json",
                raw_sha256=digest, record_id=f"{symbol}:{timeframe}:{timestamp}",
            )
            bars.append(MarketBar(provenance=provenance, interval=timeframe,
                bar_start=start, bar_end=end, open=row["o"], high=row["h"],
                low=row["l"], close=row["c"], volume=row["v"], adjusted=False))
        bars.sort(key=lambda item: item.bar_start)
        quote_provenance = DataProvenance(
            provider="crypto_com_exchange", source="wss://stream.crypto.com/exchange/v1/market",
            dataset="quote", instrument_id=instrument_id,
            observed_at=tick.provider_timestamp, published_at=None,
            received_at=tick.received_at, available_at=tick.received_at,
            revision=1, currency=quote_currency, unit=f"{quote_currency}/{base_currency}",
            raw_reference=f"crypto.com/ticker/{symbol}/{int(tick.provider_timestamp.timestamp() * 1000)}.json",
            raw_sha256=tick.payload_sha256,
            record_id=f"{symbol}:{tick.provider_timestamp.isoformat()}:{tick.payload_sha256}",
        )
        quote = Quote(provenance=quote_provenance, bid=tick.best_bid, ask=tick.best_ask,
            bid_size=tick.best_bid_size, ask_size=tick.best_ask_size, venue="CRYPTOCOM")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise LiveSignalError("Les bougies ou les règles instrument Crypto.com sont invalides.") from None
    return evaluate_breakout_signal(
        bars, quote, instrument, as_of=cutoff,
        max_signal_bar_age=duration + timedelta(seconds=30),
    )


__all__ = ["LiveSignalError", "observe_cryptocom_breakout"]
