"""Deterministic fixture event projection for paper portfolio snapshots."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, Inexact, localcontext

from invest_ai.portfolio.models import (
    MarkToMarketEvent,
    PaperFillEvent,
    PortfolioBootstrapConfig,
    PortfolioContractError,
    PortfolioEvent,
    PortfolioOrderRecord,
    PortfolioPosition,
    PortfolioSnapshot,
    ReservationReference,
    ReservationSnapshot,
    canonical_bytes,
)


@dataclass(slots=True)
class _Holding:
    instrument_id: str
    symbol: str
    quantity: Decimal
    cost_basis: Decimal
    mark_price: Decimal
    multiplier: Decimal
    currency: str
    market_snapshot_id: str

    @property
    def market_value(self) -> Decimal:
        return self.mark_price * self.quantity * self.multiplier


def _equity(cash: Decimal, holdings: dict[str, _Holding]) -> Decimal:
    return cash + sum((holding.market_value for holding in holdings.values()), Decimal("0"))


def _event_digest(event: PortfolioEvent) -> str:
    return hashlib.sha256(canonical_bytes(event)).hexdigest()


def project_portfolio(
    config: PortfolioBootstrapConfig,
    events: tuple[PortfolioEvent, ...],
    *,
    reservations: ReservationSnapshot,
    as_of: datetime,
) -> PortfolioSnapshot:
    """Project immutable events and a required external reservation view.

    This function is pure and fixture-oriented. It does not store events,
    establish that a bootstrap approval is authentic, own reservations, or
    make a transactionally current/authoritative portfolio claim.
    """
    if not isinstance(config, PortfolioBootstrapConfig):
        raise TypeError("PortfolioBootstrapConfig required.")
    if not isinstance(reservations, ReservationSnapshot):
        raise TypeError("An explicit ReservationSnapshot view is required.")
    if not isinstance(events, tuple) or any(not isinstance(event, (PaperFillEvent, MarkToMarketEvent)) for event in events):
        raise PortfolioContractError("events must be an immutable tuple of supported portfolio events.")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise PortfolioContractError("as_of must be a timezone-aware timestamp.")
    cutoff = as_of.astimezone(timezone.utc)

    unique_by_id: dict[str, tuple[str, PortfolioEvent]] = {}
    for event in events:
        digest = _event_digest(event)
        previous = unique_by_id.get(event.event_id)
        if previous is not None:
            if previous[0] != digest:
                raise PortfolioContractError(f"event ID {event.event_id!r} has contradictory contents.")
            continue
        unique_by_id[event.event_id] = (digest, event)
    ordered = sorted((pair for pair in unique_by_id.values()), key=lambda pair: pair[1].sequence)
    sequences = [event.sequence for _, event in ordered]
    if len(sequences) != len(set(sequences)):
        raise PortfolioContractError("event sequence numbers must be unique.")
    previous_time: datetime | None = None
    for _, event in ordered:
        if event.occurred_at > cutoff:
            raise PortfolioContractError(f"event {event.event_id!r} is later than snapshot as_of.")
        if previous_time is not None and event.occurred_at < previous_time:
            raise PortfolioContractError("event sequence and occurred_at order contradict one another.")
        previous_time = event.occurred_at

    for exposure in reservations.reservations:
        if exposure.currency is not None and exposure.currency != config.base_currency:
            raise PortfolioContractError("reservation currency must match portfolio base_currency.")
        if exposure.created_at > cutoff:
            raise PortfolioContractError("reservation timestamp is later than snapshot as_of.")

    with localcontext() as context:
        context.prec = 200
        cash = config.initial_cash
        realized_pnl = Decimal("0")
        peak_equity = config.initial_cash
        holdings: dict[str, _Holding] = {}
        order_history: list[PortfolioOrderRecord] = []
        seen_request_ids: set[str] = set()
        seen_proposal_ids: set[str] = set()
        seen_idempotency_keys: set[str] = set()
        current_day = None
        day_open_equity = config.initial_cash
        daily_pnl = Decimal("0")

        for _, event in ordered:
            event_day = event.occurred_at.date()
            if current_day != event_day:
                day_open_equity = _equity(cash, holdings)
                current_day = event_day

            if event.currency != config.base_currency:
                raise PortfolioContractError("event currency differs from portfolio base_currency; FX is unsupported.")

            if isinstance(event, PaperFillEvent):
                if (event.request_id in seen_request_ids or event.proposal_id in seen_proposal_ids
                        or event.idempotency_key in seen_idempotency_keys):
                    raise PortfolioContractError("a request, proposal, or idempotency key appears more than once.")
                seen_request_ids.add(event.request_id)
                seen_proposal_ids.add(event.proposal_id)
                seen_idempotency_keys.add(event.idempotency_key)
                holding = holdings.get(event.instrument_id)
                if holding and (holding.symbol != event.symbol or holding.multiplier != event.multiplier
                                or holding.currency != event.currency):
                    raise PortfolioContractError("instrument identity or units changed within the event history.")
                notional = event.fill_price * event.quantity * event.multiplier
                market_value = event.reference_price * event.quantity * event.multiplier
                if event.side == "BUY":
                    debit = notional + event.fee
                    if debit > cash:
                        raise PortfolioContractError("buy event exceeds available cash; margin is unsupported.")
                    cash -= debit
                    if holding is None:
                        holdings[event.instrument_id] = _Holding(
                            event.instrument_id, event.symbol, event.quantity,
                            notional + event.fee, event.reference_price, event.multiplier,
                            event.currency, event.market_snapshot_id,
                        )
                    else:
                        holding.quantity += event.quantity
                        holding.cost_basis += notional + event.fee
                        holding.mark_price = event.reference_price
                        holding.market_snapshot_id = event.market_snapshot_id
                else:
                    if holding is None or event.quantity > holding.quantity:
                        raise PortfolioContractError("sell event exceeds the existing long position; shorting is unsupported.")
                    proceeds = notional - event.fee
                    if proceeds < 0:
                        raise PortfolioContractError("sell fee exceeds gross proceeds.")
                    try:
                        with localcontext() as allocation_context:
                            allocation_context.prec = 200
                            allocation_context.traps[Inexact] = True
                            allocated_cost = holding.cost_basis * event.quantity / holding.quantity
                    except Inexact:
                        raise PortfolioContractError(
                            "cost basis allocation would require an unspecified rounding rule; event refused."
                        ) from None
                    realized_pnl += proceeds - allocated_cost
                    cash += proceeds
                    holding.quantity -= event.quantity
                    holding.cost_basis -= allocated_cost
                    if holding.quantity == 0:
                        del holdings[event.instrument_id]
                    else:
                        holding.mark_price = event.reference_price
                        holding.market_snapshot_id = event.market_snapshot_id

                order_history.append(PortfolioOrderRecord(
                    event.event_id, event.request_id, event.proposal_id, event.idempotency_key,
                    event.instrument_id, event.symbol, event.side, event.quantity,
                    event.fill_price, event.fee, event.currency, event.occurred_at,
                    event.market_snapshot_id,
                ))
            else:
                holding = holdings.get(event.instrument_id)
                if holding is None:
                    raise PortfolioContractError("mark event references an instrument with no open position.")
                if holding.symbol != event.symbol or holding.multiplier != event.multiplier:
                    raise PortfolioContractError("mark event instrument identity or multiplier changed.")
                holding.mark_price = event.price
                holding.market_snapshot_id = event.market_snapshot_id

            current_equity = _equity(cash, holdings)
            peak_equity = max(peak_equity, current_equity)
            daily_pnl = current_equity - day_open_equity

        if current_day != cutoff.date():
            daily_pnl = Decimal("0")

        if reservations.reservations:
            completed_proposals = {record.proposal_id for record in order_history}
            completed_request_keys = {record.idempotency_key for record in order_history}
            if any(x.proposal_id in completed_proposals for x in reservations.reservations):
                raise PortfolioContractError("reservation duplicates a proposal already filled in this projection.")
            if any(x.idempotency_key is not None and x.idempotency_key in completed_request_keys
                   for x in reservations.reservations):
                raise PortfolioContractError("reservation duplicates an idempotency key already filled in this projection.")

        positions = tuple(
            PortfolioPosition(
                holding.instrument_id, holding.symbol, holding.quantity, holding.cost_basis,
                holding.market_value, holding.mark_price, holding.multiplier, holding.currency,
                holding.market_snapshot_id,
            )
            for holding in sorted(holdings.values(), key=lambda item: item.instrument_id)
        )
        equity = _equity(cash, holdings)
        unrealized_pnl = sum((position.market_value - position.cost_basis for position in positions), Decimal("0"))
        total_pnl = equity - config.initial_cash
        event_refs = tuple(event.event_id for _, event in ordered)
        event_digests = tuple(digest for digest, _ in ordered)
        snapshot_values = {
            "portfolio_id": config.portfolio_id,
            "base_currency": config.base_currency,
            "config_digest": config.config_digest,
            "approval_record_id": config.approval_record_id,
            "approval_record_digest": config.approval_record_digest,
            "as_of": cutoff,
            "revision": len(ordered),
            "reservation_source_id": reservations.source_id,
            "reservation_revision": reservations.revision,
            "reservation_digest": reservations.digest,
            "initial_cash": config.initial_cash,
            "cash": cash,
            "equity": equity,
            "peak_equity": peak_equity,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,
            "daily_pnl": daily_pnl,
            "positions": positions,
            "pending_exposures": reservations.reservations,
            "order_history": tuple(order_history),
            "event_refs": event_refs,
            "event_digests": event_digests,
            "authority_status": "FIXTURE_PROJECTION_ONLY",
        }
        digest = hashlib.sha256(canonical_bytes(snapshot_values)).hexdigest()
        return PortfolioSnapshot(
            **snapshot_values,
            digest=digest,
            snapshot_id=f"paper-portfolio:{digest}",
        )


__all__ = ["project_portfolio"]
