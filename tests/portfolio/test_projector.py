from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from invest_ai.portfolio import (
    MarkToMarketEvent,
    PaperFillEvent,
    PortfolioBootstrapConfig,
    PortfolioContractError,
    ReservationReference,
    ReservationSnapshot,
    ReservationStatus,
    compute_bootstrap_config_digest,
    compute_reservation_snapshot_digest,
    project_portfolio,
)


NOW = datetime(2025, 1, 3, 15, 0, tzinfo=timezone.utc)
APPROVAL_DIGEST = hashlib.sha256(b"synthetic test-only approval record").hexdigest()


def config(*, initial_cash: Decimal = Decimal("1000.00")) -> PortfolioBootstrapConfig:
    approval_id = "TEST_ONLY:synthetic-bootstrap-approval"
    digest = compute_bootstrap_config_digest("portfolio-test", "EUR", initial_cash, approval_id, APPROVAL_DIGEST)
    return PortfolioBootstrapConfig("portfolio-test", "EUR", initial_cash, approval_id, APPROVAL_DIGEST, digest)


def reservation_view(*items: ReservationReference, revision: int = 1) -> ReservationSnapshot:
    rows = tuple(items)
    digest = compute_reservation_snapshot_digest("orchestration-test-view", revision, rows)
    return ReservationSnapshot("orchestration-test-view", revision, rows, digest)


def buy_event(*, event_id: str = "event-buy", sequence: int = 1,
              occurred_at: datetime = NOW, quantity: Decimal = Decimal("2")) -> PaperFillEvent:
    return PaperFillEvent(
        event_id, sequence, "request-buy-1", "proposal-buy-1", "idem-buy-1",
        "instrument-1", "TEST", "BUY", quantity, Decimal("101"), Decimal("100"),
        Decimal("1"), Decimal("1"), "EUR", "market-1", occurred_at,
    )


def mark_event(*, event_id: str = "event-mark", sequence: int = 2,
               occurred_at: datetime = NOW + timedelta(minutes=1), price: Decimal = Decimal("102")) -> MarkToMarketEvent:
    return MarkToMarketEvent(
        event_id, sequence, "instrument-1", "TEST", price, "EUR", Decimal("1"),
        "market-2", occurred_at,
    )


class PortfolioProjectorTests(unittest.TestCase):
    def test_fill_and_mark_produce_coherent_immutable_snapshot(self) -> None:
        result = project_portfolio(config(), (buy_event(), mark_event()), reservations=reservation_view(),
                                   as_of=NOW + timedelta(minutes=2))
        self.assertEqual(result.cash, Decimal("797.00"))
        self.assertEqual(result.equity, Decimal("1001.00"))
        self.assertEqual(result.peak_equity, Decimal("1001.00"))
        self.assertEqual(result.realized_pnl, Decimal("0"))
        self.assertEqual(result.unrealized_pnl, Decimal("1.00"))
        self.assertEqual(result.total_pnl, Decimal("1.00"))
        self.assertEqual(result.daily_pnl, Decimal("1.00"))
        self.assertEqual(result.positions[0].quantity, Decimal("2"))
        self.assertEqual(result.positions[0].market_value, Decimal("204"))
        self.assertEqual(result.revision, 2)
        self.assertEqual(result.event_refs, ("event-buy", "event-mark"))
        self.assertEqual(result.authority_status, "FIXTURE_PROJECTION_ONLY")

    def test_sell_tracks_realized_and_unrealized_pnl(self) -> None:
        sell = PaperFillEvent(
            "event-sell", 3, "request-sell-1", "proposal-sell-1", "idem-sell-1",
            "instrument-1", "TEST", "SELL", Decimal("1"), Decimal("103"), Decimal("102"),
            Decimal("1"), Decimal("1"), "EUR", "market-3", NOW + timedelta(minutes=2),
        )
        result = project_portfolio(config(), (buy_event(), mark_event(), sell), reservations=reservation_view(),
                                   as_of=NOW + timedelta(minutes=3))
        self.assertEqual(result.cash, Decimal("899.00"))
        self.assertEqual(result.positions[0].quantity, Decimal("1"))
        self.assertEqual(result.realized_pnl, Decimal("0.50"))
        self.assertEqual(result.unrealized_pnl, Decimal("0.50"))
        self.assertEqual(result.total_pnl, Decimal("1.00"))
        self.assertEqual(len(result.order_history), 2)

    def test_identical_duplicate_event_is_idempotent(self) -> None:
        one = project_portfolio(config(), (buy_event(),), reservations=reservation_view(),
                                as_of=NOW + timedelta(minutes=1))
        two = project_portfolio(config(), (buy_event(), buy_event()), reservations=reservation_view(),
                                as_of=NOW + timedelta(minutes=1))
        self.assertEqual(one, two)

    def test_contradictory_duplicate_event_id_is_rejected(self) -> None:
        contradictory = replace(buy_event(), quantity=Decimal("3"))
        with self.assertRaises(PortfolioContractError):
            project_portfolio(config(), (buy_event(), contradictory), reservations=reservation_view(),
                              as_of=NOW + timedelta(minutes=1))

    def test_duplicate_request_proposal_or_idempotency_key_is_rejected(self) -> None:
        other = replace(buy_event(event_id="event-buy-2", sequence=2), event_id="event-buy-2",
                        request_id="request-buy-1", proposal_id="proposal-buy-2", idempotency_key="idem-buy-2")
        with self.assertRaises(PortfolioContractError):
            project_portfolio(config(), (buy_event(), other), reservations=reservation_view(),
                              as_of=NOW + timedelta(minutes=1))

    def test_sell_cannot_create_short_position(self) -> None:
        sell = PaperFillEvent(
            "event-sell", 1, "request-sell", "proposal-sell", "idem-sell", "instrument-1", "TEST",
            "SELL", Decimal("1"), Decimal("100"), Decimal("100"), Decimal("0"), Decimal("1"),
            "EUR", "market-1", NOW,
        )
        with self.assertRaisesRegex(PortfolioContractError, "shorting is unsupported"):
            project_portfolio(config(), (sell,), reservations=reservation_view(), as_of=NOW)

    def test_partial_cost_allocation_fails_closed_when_rounding_would_be_needed(self) -> None:
        buy = replace(buy_event(), quantity=Decimal("3"), fee=Decimal("1"))
        sell = PaperFillEvent(
            "event-sell", 2, "request-sell", "proposal-sell", "idem-sell", "instrument-1", "TEST",
            "SELL", Decimal("1"), Decimal("101"), Decimal("100"), Decimal("0"), Decimal("1"),
            "EUR", "market-2", NOW + timedelta(minutes=1),
        )
        with self.assertRaisesRegex(PortfolioContractError, "rounding rule"):
            project_portfolio(config(), (buy, sell), reservations=reservation_view(),
                              as_of=NOW + timedelta(minutes=2))

    def test_buy_cannot_exceed_opening_cash(self) -> None:
        with self.assertRaisesRegex(PortfolioContractError, "margin is unsupported"):
            project_portfolio(config(), (buy_event(quantity=Decimal("10")),), reservations=reservation_view(),
                              as_of=NOW + timedelta(minutes=1))

    def test_currency_mismatch_fails_closed(self) -> None:
        bad_currency = replace(buy_event(), currency="USD")
        with self.assertRaises(PortfolioContractError):
            project_portfolio(config(), (bad_currency,), reservations=reservation_view(),
                              as_of=NOW + timedelta(minutes=1))

    def test_future_or_out_of_order_event_is_rejected(self) -> None:
        future = replace(buy_event(), occurred_at=NOW + timedelta(hours=1))
        with self.assertRaises(PortfolioContractError):
            project_portfolio(config(), (future,), reservations=reservation_view(), as_of=NOW)
        backdated = replace(mark_event(), occurred_at=NOW - timedelta(minutes=1))
        with self.assertRaises(PortfolioContractError):
            project_portfolio(config(), (buy_event(), backdated), reservations=reservation_view(),
                              as_of=NOW + timedelta(minutes=2))

    def test_invalid_decimal_float_and_digest_are_rejected(self) -> None:
        with self.assertRaises(PortfolioContractError):
            config(initial_cash=1000.0)  # type: ignore[arg-type]
        with self.assertRaises(PortfolioContractError):
            replace(config(), initial_cash=Decimal("999"))
        with self.assertRaises(PortfolioContractError):
            replace(reservation_view(), digest="0" * 64)
        with self.assertRaises(PortfolioContractError):
            ReservationReference("proposal-x", Decimal("5"), "bucket",
                                 ReservationStatus.RELEASED, NOW)

    def test_reservations_are_sorted_and_preserved_as_external_view(self) -> None:
        z = ReservationReference("proposal-z", Decimal("40"), "global", ReservationStatus.RESERVED, NOW)
        a = ReservationReference("proposal-a", Decimal("20"), "global", ReservationStatus.RESERVED, NOW)
        view1 = reservation_view(z, a)
        view2 = reservation_view(a, z)
        self.assertEqual(view1.digest, view2.digest)
        result = project_portfolio(config(), (), reservations=view1, as_of=NOW)
        self.assertEqual([item.proposal_id for item in result.pending_exposures], ["proposal-a", "proposal-z"])
        self.assertEqual(result.reservation_source_id, "orchestration-test-view")

    def test_reservation_for_filled_proposal_is_rejected(self) -> None:
        active = ReservationReference("proposal-buy-1", Decimal("200"), "global", ReservationStatus.RESERVED, NOW)
        with self.assertRaises(PortfolioContractError):
            project_portfolio(config(), (buy_event(),), reservations=reservation_view(active),
                              as_of=NOW + timedelta(minutes=1))

    def test_risk_conversion_refuses_reservation_without_units(self) -> None:
        active = ReservationReference("proposal-pending", Decimal("50"), "global", ReservationStatus.RESERVED, NOW)
        result = project_portfolio(config(), (), reservations=reservation_view(active), as_of=NOW)
        with self.assertRaisesRegex(PortfolioContractError, "instrument_id and matching currency"):
            result.to_risk_state()

    def test_risk_conversion_includes_typed_positions_and_reservations(self) -> None:
        active = ReservationReference("proposal-pending", Decimal("50"), "global", ReservationStatus.RESERVED, NOW,
                                      instrument_id="instrument-2", currency="EUR")
        result = project_portfolio(config(), (buy_event(),), reservations=reservation_view(active),
                                   as_of=NOW + timedelta(minutes=1))
        risk_state = result.to_risk_state()
        self.assertEqual(risk_state.base_currency, "EUR")
        self.assertEqual(risk_state.positions[0].instrument_id, "instrument-1")
        self.assertEqual(risk_state.pending_exposures[0].instrument_id, "instrument-2")
        self.assertEqual(risk_state.pending_exposures[0].notional, Decimal("50"))

    def test_snapshot_digest_detects_field_tampering(self) -> None:
        result = project_portfolio(config(), (), reservations=reservation_view(), as_of=NOW)
        with self.assertRaisesRegex(PortfolioContractError, "digest does not match"):
            replace(result, approval_record_id="different-approval-reference")

    def test_same_inputs_are_reproducible_and_digest_is_content_addressed(self) -> None:
        args = (config(), (buy_event(), mark_event()))
        kwargs = {"reservations": reservation_view(), "as_of": NOW + timedelta(minutes=2)}
        first = project_portfolio(*args, **kwargs)
        second = project_portfolio(*args, **kwargs)
        self.assertEqual(first, second)
        self.assertEqual(first.snapshot_id, f"paper-portfolio:{first.digest}")

    def test_projection_is_independent_of_global_decimal_precision(self) -> None:
        args = (config(), (buy_event(), mark_event()))
        kwargs = {"reservations": reservation_view(), "as_of": NOW + timedelta(minutes=2)}
        baseline = project_portfolio(*args, **kwargs)
        with localcontext() as context:
            context.prec = 3
            low_precision = project_portfolio(*args, **kwargs)
        self.assertEqual(baseline, low_precision)


if __name__ == "__main__":
    unittest.main()
