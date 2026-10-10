from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from invest_ai.data.models import DataProvenance, Instrument, MarketBar, Quote
from invest_ai.strategy import (BreakoutPolicy, SignalStatus, StrategyContractError,
                                evaluate_breakout, evaluate_breakout_signal)


BASE = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)


def instrument(*, tick: str | None = "0.01", lot: str | None = "1", currency: str = "USD") -> Instrument:
    return Instrument(
        "FIXTURE-SYNX", "SYNX", "EQUITY", currency, exchange="XNAS", venue="XNAS",
        tick_size=Decimal(tick) if tick is not None else None,
        lot_size=Decimal(lot) if lot is not None else None,
        price_convention="PER_SHARE",
    )


def provenance(record: str, at: datetime, dataset: str, *, provider: str = "SYNTHETIC_FIXTURE",
               currency: str = "USD") -> DataProvenance:
    digest = (record.encode("utf-8").hex()[:64]).ljust(64, "0")
    return DataProvenance(
        provider=provider, source="fixture://strategy-test", dataset=dataset,
        instrument_id="FIXTURE-SYNX", observed_at=at, published_at=at,
        received_at=at + timedelta(milliseconds=500),
        available_at=at + timedelta(milliseconds=500), revision=1,
        currency=currency, unit="shares", raw_reference=f"fixtures/strategy/{record}.json",
        raw_sha256=digest, record_id=record,
    )


def make_bars(*, direction: str = "UP", stale: bool = False, provider: str = "SYNTHETIC_FIXTURE",
              adjusted_last: bool | None = False) -> list[MarketBar]:
    if direction == "UP":
        prices = (
            ("100.00", "100.10", "99.90", "1000"),
            ("100.20", "100.30", "100.10", "950"),
            ("100.50", "100.60", "100.40", "1020"),
            ("100.50", "105.10", "100.40", "105.00", "3500"),
        )
    else:
        prices = (
            ("105.00", "105.10", "104.90", "1000"),
            ("104.70", "104.90", "104.60", "950"),
            ("104.50", "104.70", "104.30", "1020"),
            ("104.50", "104.60", "98.40", "98.50", "3500"),
        )
    result = []
    for index, values in enumerate(prices):
        observed = BASE + timedelta(seconds=index * 5)
        if stale:
            observed -= timedelta(minutes=2)
        if len(values) == 4:
            open_text, high_text, low_text, volume_text = values
            close_text = open_text
        else:
            open_text, high_text, low_text, close_text, volume_text = values
        adjusted = adjusted_last if index == len(prices) - 1 else False
        result.append(MarketBar(
            provenance=provenance(f"bar-{direction.lower()}-{index}", observed, "market_bar", provider=provider),
            interval="5s", bar_start=observed - timedelta(seconds=5), bar_end=observed,
            open=Decimal(open_text), high=Decimal(high_text), low=Decimal(low_text),
            close=Decimal(close_text), volume=Decimal(volume_text), adjusted=adjusted,
        ))
    return result


def make_quote(*, direction: str = "UP", spread: str = "0.02", sizes: tuple[str, str] = ("120", "100"),
               at: datetime | None = None, provider: str = "SYNTHETIC_FIXTURE") -> Quote:
    at = at or BASE + timedelta(seconds=16)
    if direction == "UP":
        midpoint = Decimal("105.00")
        bid_size, ask_size = sizes
    else:
        midpoint = Decimal("98.50")
        bid_size, ask_size = ("100", "120") if sizes == ("120", "100") else sizes
    half = Decimal(spread) / 2
    return Quote(
        provenance=provenance(f"quote-{direction.lower()}", at, "quote", provider=provider),
        bid=midpoint - half, ask=midpoint + half,
        bid_size=Decimal(bid_size), ask_size=Decimal(ask_size), venue="XNAS",
    )


class BreakoutCandidateTests(unittest.TestCase):
    def evaluate(self, *, direction: str = "UP", position: str = "0", policy: BreakoutPolicy | None = None,
                 instrument_value: Instrument | None = None, quote: Quote | None = None,
                 bars: list[MarketBar] | None = None, status: str = "SYNTHETIC_FIXTURE",
                 as_of: datetime | None = None):
        bars = bars if bars is not None else make_bars(direction=direction)
        quote = quote if quote is not None else make_quote(direction=direction)
        current = as_of or quote.provenance.received_at + timedelta(milliseconds=500)
        return evaluate_breakout(
            bars, quote, instrument_value or instrument(), position_quantity=Decimal(position),
            portfolio_context_ref="FIXTURE-PORTFOLIO-ASSUMPTION-1",
            policy=policy or BreakoutPolicy(), market_data_status=status, as_of=current,
        )

    def test_upward_breakout_creates_complete_but_non_authoritative_buy_draft(self) -> None:
        result = self.evaluate()
        self.assertEqual(result.status, SignalStatus.PROPOSAL_CANDIDATE)
        draft = result.candidate
        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual((draft.side, draft.quantity), ("BUY", Decimal("9")))
        self.assertEqual(draft.order_type, "LIMIT")
        self.assertEqual(draft.limit_price, Decimal("105.01"))
        self.assertEqual(draft.stop_price, Decimal("102.90"))
        self.assertEqual(draft.take_profit_price, Decimal("109.22"))
        self.assertEqual(draft.estimated_loss_at_stop, Decimal("18.99"))
        self.assertEqual(draft.estimated_notional, Decimal("945.09"))
        self.assertEqual(draft.authority_status, "RESEARCH_CANDIDATE_ONLY")
        self.assertEqual(draft.risk_evaluation_status, "NOT_EVALUATED")
        self.assertFalse(draft.execution_allowed)
        self.assertFalse(draft.broker_order_sent)
        self.assertEqual(len(draft.evidence_refs), 5)
        self.assertIn("Hypothèse de recherche", draft.rationale)

    def test_crypto_com_realtime_evidence_can_reach_research_candidate_only(self) -> None:
        crypto_instrument = Instrument("crypto.com:BTC_EUR", "BTC_EUR", "CRYPTO", "EUR",
            exchange="Crypto.com Exchange", venue="CRYPTOCOM", instrument_type="SPOT",
            tick_size=Decimal("0.01"), lot_size=Decimal("0.00001"))
        crypto_bars = make_bars(provider="crypto_com_exchange")
        crypto_bars = [replace(bar, provenance=replace(bar.provenance,
            instrument_id=crypto_instrument.instrument_id, currency="EUR", unit="EUR/BTC"))
            for bar in crypto_bars]
        crypto_quote = make_quote(provider="crypto_com_exchange")
        crypto_quote = replace(crypto_quote, venue="CRYPTOCOM", provenance=replace(
            crypto_quote.provenance, instrument_id=crypto_instrument.instrument_id,
            currency="EUR", unit="EUR/BTC"))
        result = evaluate_breakout(crypto_bars, crypto_quote, crypto_instrument,
            position_quantity=Decimal("0"), portfolio_context_ref="CRYPTO-SPOT-OBSERVATION",
            policy=BreakoutPolicy(budget_currency="EUR"), market_data_status="REALTIME",
            as_of=crypto_quote.provenance.received_at + timedelta(milliseconds=500))
        self.assertEqual(result.status, SignalStatus.PROPOSAL_CANDIDATE)
        self.assertEqual(result.candidate.authority_status, "RESEARCH_CANDIDATE_ONLY")
        self.assertFalse(result.candidate.execution_allowed)

    def test_crypto_com_five_minute_history_allows_older_lookback_bars_and_usdt(self) -> None:
        crypto_instrument = Instrument("crypto.com:BTC_USDT", "BTC_USDT", "CRYPTO", "USDT",
            exchange="Crypto.com Exchange", venue="CRYPTOCOM", instrument_type="SPOT",
            tick_size=Decimal("0.01"), lot_size=Decimal("0.00001"))
        source_bars = make_bars(provider="crypto_com_exchange")
        start = BASE - timedelta(minutes=20)
        crypto_bars = []
        for index, source in enumerate(source_bars):
            bar_start = start + timedelta(minutes=index * 5)
            bar_end = bar_start + timedelta(minutes=5)
            received = bar_end + timedelta(milliseconds=500)
            evidence = replace(source.provenance, instrument_id=crypto_instrument.instrument_id,
                currency="USDT", unit="USDT/BTC", observed_at=bar_end, published_at=bar_end,
                received_at=received, available_at=received)
            crypto_bars.append(replace(source, provenance=evidence, interval="5m",
                bar_start=bar_start, bar_end=bar_end))
        quote = make_quote(provider="crypto_com_exchange", at=BASE + timedelta(seconds=1))
        quote = replace(quote, venue="CRYPTOCOM", provenance=replace(
            quote.provenance, instrument_id=crypto_instrument.instrument_id,
            currency="USDT", unit="USDT/BTC"))
        result = evaluate_breakout(crypto_bars, quote, crypto_instrument,
            position_quantity=Decimal("0"), portfolio_context_ref="CRYPTO-SPOT-OBSERVATION",
            policy=BreakoutPolicy(budget_currency="USDT", max_bar_age=timedelta(minutes=5)),
            market_data_status="REALTIME", as_of=BASE + timedelta(seconds=2))
        self.assertEqual(result.status, SignalStatus.PROPOSAL_CANDIDATE)
        self.assertEqual(result.candidate.currency, "USDT")
        self.assertFalse(result.candidate.execution_allowed)

    def test_breakout_rejects_mixed_or_gapped_lookback_windows(self) -> None:
        bars = make_bars()
        mixed = list(bars)
        mixed[1] = replace(mixed[1], interval="1m")
        self.assertEqual(self.evaluate(bars=mixed).reason_codes, ("MIXED_BAR_INTERVALS",))
        gapped = list(bars)
        gapped[2] = replace(gapped[2], bar_start=gapped[2].bar_start + timedelta(seconds=1))
        self.assertEqual(self.evaluate(bars=gapped).reason_codes, ("GAPPED_OR_OVERLAPPING_BARS",))

    def test_live_signal_observation_has_no_portfolio_or_order_sizing(self) -> None:
        crypto_instrument = Instrument("crypto.com:BTC_USDT", "BTC_USDT", "CRYPTO", "USDT",
            exchange="Crypto.com Exchange", venue="CRYPTOCOM", instrument_type="SPOT",
            tick_size=Decimal("0.01"), lot_size=Decimal("0.00001"))
        source_bars = make_bars(provider="crypto_com_exchange")
        start = BASE - timedelta(minutes=20)
        closed_bars = []
        for index, source in enumerate(source_bars):
            bar_start = start + timedelta(minutes=index * 5)
            bar_end = bar_start + timedelta(minutes=5)
            received = bar_end + timedelta(milliseconds=500)
            evidence = replace(source.provenance, instrument_id=crypto_instrument.instrument_id,
                currency="USDT", unit="USDT/BTC", observed_at=bar_end, published_at=None,
                received_at=received, available_at=received)
            closed_bars.append(replace(source, provenance=evidence, interval="5m",
                bar_start=bar_start, bar_end=bar_end))
        quote = make_quote(provider="crypto_com_exchange", at=BASE + timedelta(seconds=1))
        quote = replace(quote, venue="CRYPTOCOM", provenance=replace(
            quote.provenance, instrument_id=crypto_instrument.instrument_id,
            currency="USDT", unit="USDT/BTC"))
        result = evaluate_breakout_signal(closed_bars, quote, crypto_instrument,
            as_of=BASE + timedelta(seconds=2), max_signal_bar_age=timedelta(minutes=5))
        self.assertEqual(result.status, SignalStatus.SIGNAL_OBSERVED)
        self.assertEqual(result.direction, "UP_BREAKOUT")
        self.assertEqual(result.authority_status, "OBSERVATION_ONLY")
        self.assertFalse(result.execution_allowed)
        self.assertFalse(hasattr(result, "quantity"))
        self.assertEqual(len(result.evidence_refs), 5)

    def test_live_signal_fails_closed_without_a_fresh_complete_quote(self) -> None:
        bars = make_bars(provider="crypto_com_exchange")
        source_quote = make_quote(provider="crypto_com_exchange")
        inst = Instrument("crypto.com:BTC_USD", "BTC_USD", "CRYPTO", "USD",
            exchange="Crypto.com Exchange", venue="CRYPTOCOM", instrument_type="SPOT",
            tick_size=Decimal("0.01"), lot_size=Decimal("0.00001"))
        bars = [replace(bar, provenance=replace(bar.provenance,
                    instrument_id=inst.instrument_id)) for bar in bars]
        quote = replace(source_quote, venue="CRYPTOCOM", provenance=replace(
            source_quote.provenance, instrument_id=inst.instrument_id))
        incomplete = replace(quote, ask=None)
        result = evaluate_breakout_signal(bars, incomplete, inst,
            as_of=BASE + timedelta(seconds=18))
        self.assertEqual(result.status, SignalStatus.BLOCKED)
        self.assertIn(result.reason_codes[0], {"INCOMPLETE_OR_CROSSED_QUOTE", "STALE_QUOTE_DELIVERY"})

    def test_downward_breakout_proposes_only_reducing_an_existing_long_position(self) -> None:
        result = self.evaluate(direction="DOWN", position="5")
        self.assertEqual(result.status, SignalStatus.PROPOSAL_CANDIDATE)
        draft = result.candidate
        assert draft is not None
        self.assertEqual((draft.side, draft.quantity), ("SELL", Decimal("5")))
        self.assertEqual(draft.limit_price, Decimal("98.49"))
        self.assertGreater(draft.stop_price, draft.limit_price)
        self.assertLess(draft.take_profit_price, draft.limit_price)

    def test_downward_breakout_never_proposes_an_uncovered_short_sale(self) -> None:
        result = self.evaluate(direction="DOWN", position="0")
        self.assertEqual(result.status, SignalStatus.NO_SIGNAL)
        self.assertEqual(result.reason_codes, ("NO_LONG_POSITION_TO_SELL",))
        self.assertIsNone(result.candidate)

    def test_currency_conversion_must_be_explicit(self) -> None:
        result = self.evaluate(instrument_value=instrument(currency="EUR"))
        self.assertEqual(result.status, SignalStatus.BLOCKED)
        self.assertEqual(result.reason_codes, ("CURRENCY_CONVERSION_NOT_CONFIGURED",))

    def test_missing_tick_or_lot_metadata_fails_closed(self) -> None:
        for item in (instrument(tick=None), instrument(lot=None)):
            with self.subTest(item=item):
                result = self.evaluate(instrument_value=item)
                self.assertEqual(result.reason_codes, ("TICK_AND_LOT_METADATA_REQUIRED",))
                self.assertIsNone(result.candidate)

    def test_delayed_data_is_rejected_before_candidate_creation(self) -> None:
        result = self.evaluate(status="DELAYED")
        self.assertEqual(result.status, SignalStatus.BLOCKED)
        self.assertEqual(result.reason_codes, ("MARKET_DATA_NOT_REALTIME_OR_SYNTHETIC_FIXTURE",))

    def test_real_time_status_requires_ibkr_provenance_and_synthetic_status_requires_fixture(self) -> None:
        result = self.evaluate(status="REALTIME")
        self.assertEqual(result.reason_codes, ("REALTIME_STATUS_PROVIDER_MISMATCH",))
        wrong_provider_bars = make_bars(provider="IBKR")
        wrong_provider_quote = make_quote(provider="IBKR")
        fixture_claim = self.evaluate(bars=wrong_provider_bars, quote=wrong_provider_quote)
        self.assertEqual(fixture_claim.reason_codes, ("SYNTHETIC_STATUS_PROVIDER_MISMATCH",))

    def test_stale_or_unknown_adjustment_history_is_rejected(self) -> None:
        stale_bars = make_bars(stale=True)
        stale_quote = make_quote(at=BASE - timedelta(minutes=1))
        stale = self.evaluate(bars=stale_bars, quote=stale_quote,
                              as_of=BASE + timedelta(seconds=2))
        self.assertIn(stale.status, {SignalStatus.BLOCKED})
        self.assertIn(stale.reason_codes[0], {"POINT_IN_TIME_OR_INSTRUMENT_VALIDATION_FAILED", "STALE_MARKET_DATA"})
        unknown = self.evaluate(bars=make_bars(adjusted_last=None))
        self.assertEqual(unknown.reason_codes, ("BAR_ADJUSTMENT_OR_VOLUME_UNKNOWN",))

    def test_missing_book_sizes_or_wide_spread_suppresses_candidate(self) -> None:
        no_sizes = make_quote(sizes=("0", "0"))
        # The canonical Quote permits zero size, but the strategy refuses a zero-depth top-of-book.
        no_size_result = self.evaluate(quote=no_sizes)
        self.assertEqual(no_size_result.reason_codes, ("TOP_OF_BOOK_SIZES_INVALID",))
        wide = self.evaluate(quote=make_quote(spread="0.50"))
        self.assertEqual(wide.status, SignalStatus.NO_SIGNAL)
        self.assertEqual(wide.reason_codes, ("SPREAD_ABOVE_CONFIGURED_LIMIT",))

    def test_insufficient_history_and_orderedness_are_checked(self) -> None:
        short = self.evaluate(bars=make_bars()[:3])
        self.assertEqual(short.reason_codes, ("INSUFFICIENT_HISTORY",))
        ordered = make_bars()
        ordered[-1] = MarketBar(
            provenance=provenance("duplicate-time", ordered[-2].bar_end, "market_bar"),
            interval="5s", bar_start=ordered[-2].bar_start, bar_end=ordered[-2].bar_end,
            open=Decimal("100.50"), high=Decimal("105.10"), low=Decimal("100.40"),
            close=Decimal("105.00"), volume=Decimal("3500"), adjusted=False,
        )
        duplicate = self.evaluate(bars=ordered)
        self.assertEqual(duplicate.reason_codes, ("OUT_OF_ORDER_OR_DUPLICATE_BARS",))

    def test_same_inputs_have_stable_candidate_identity(self) -> None:
        first = self.evaluate().candidate
        second = self.evaluate().candidate
        self.assertIsNotNone(first)
        self.assertEqual(first.proposal_id, second.proposal_id)
        self.assertEqual(len(first.proposal_id), 64)

    def test_policy_rejects_float_thresholds(self) -> None:
        with self.assertRaises(StrategyContractError):
            BreakoutPolicy(max_order_notional=1000.0)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
