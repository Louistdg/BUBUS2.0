"""Immutable contracts for a deterministic, local PAPER portfolio projection."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PortfolioContractError(ValueError):
    """Portfolio input is malformed, contradictory, or outside V0 scope."""


class PortfolioEventType(StrEnum):
    PAPER_FILL = "PAPER_FILL"
    MARK_TO_MARKET = "MARK_TO_MARKET"


class ReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    RELEASED = "RELEASED"


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortfolioContractError(f"{field} must be a non-empty string.")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PortfolioContractError(f"{field} must be lowercase SHA-256 hex.")
    return value


def _decimal(value: object, field: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PortfolioContractError(f"{field} must be a finite Decimal; floats are forbidden.")
    if len(value.as_tuple().digits) > 50 or abs(value.as_tuple().exponent) > 50:
        raise PortfolioContractError(f"{field} exceeds the V0 Decimal bounds.")
    if positive and value <= 0:
        raise PortfolioContractError(f"{field} must be greater than zero.")
    if nonnegative and value < 0:
        raise PortfolioContractError(f"{field} must not be negative.")
    return value


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PortfolioContractError(f"{field} must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    sign, digits, exponent = value.as_tuple()
    coefficient = "".join(str(digit) for digit in digits)
    while len(coefficient) > 1 and coefficient.endswith("0"):
        coefficient = coefficient[:-1]
        exponent += 1
    prefix = "-" if sign else ""
    if exponent >= 0:
        return prefix + coefficient + ("0" * exponent)
    split = len(coefficient) + exponent
    if split > 0:
        whole, fraction = coefficient[:split], coefficient[split:]
    else:
        whole, fraction = "0", ("0" * -split) + coefficient
    return prefix + whole + "." + fraction


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_text(value)}
    if isinstance(value, datetime):
        return {"$datetime": _utc(value, "timestamp").isoformat(timespec="microseconds").replace("+00:00", "Z")}
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "__dataclass_fields__") and not isinstance(value, type):
        return {name: _canonical(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Unsupported portfolio hash value: {type(value).__name__}.")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def compute_bootstrap_config_digest(
    portfolio_id: str,
    base_currency: str,
    initial_cash: Decimal,
    approval_record_id: str,
    approval_record_digest: str,
) -> str:
    """Content digest for explicit bootstrap configuration; not an approval signature."""
    _text(portfolio_id, "portfolio_id")
    if not isinstance(base_currency, str) or re.fullmatch(r"[A-Z]{3}", base_currency) is None:
        raise PortfolioContractError("base_currency must be an uppercase three-letter currency.")
    _decimal(initial_cash, "initial_cash", nonnegative=True)
    _text(approval_record_id, "approval_record_id")
    _digest(approval_record_digest, "approval_record_digest")
    value = {
        "portfolio_id": portfolio_id,
        "base_currency": base_currency,
        "initial_cash": initial_cash,
        "approval_record_id": approval_record_id,
        "approval_record_digest": approval_record_digest,
    }
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class PortfolioBootstrapConfig:
    """Explicit opening balance and pointers to its separately approved record.

    V0 verifies content consistency only. It cannot authenticate the referenced
    approval record; synthetic fixtures must use unmistakably test-only refs.
    """

    portfolio_id: str
    base_currency: str
    initial_cash: Decimal
    approval_record_id: str
    approval_record_digest: str
    config_digest: str

    def __post_init__(self) -> None:
        _text(self.portfolio_id, "portfolio_id")
        if not isinstance(self.base_currency, str) or re.fullmatch(r"[A-Z]{3}", self.base_currency) is None:
            raise PortfolioContractError("base_currency must be an uppercase three-letter currency.")
        _decimal(self.initial_cash, "initial_cash", nonnegative=True)
        _text(self.approval_record_id, "approval_record_id")
        _digest(self.approval_record_digest, "approval_record_digest")
        _digest(self.config_digest, "config_digest")
        expected = compute_bootstrap_config_digest(
            self.portfolio_id, self.base_currency, self.initial_cash,
            self.approval_record_id, self.approval_record_digest,
        )
        if self.config_digest != expected:
            raise PortfolioContractError("bootstrap config digest does not match its contents.")


@dataclass(frozen=True, slots=True)
class ReservationReference:
    """Read-only copy of a reservation row; it does not create or own it."""

    proposal_id: str
    notional: Decimal
    bucket: str
    status: ReservationStatus
    created_at: datetime
    instrument_id: str | None = None
    currency: str | None = None
    idempotency_key: str | None = None
    sector: str | None = None

    def __post_init__(self) -> None:
        for field in ("proposal_id", "bucket"):
            _text(getattr(self, field), field)
        if self.status is not ReservationStatus.RESERVED:
            raise PortfolioContractError("only currently RESERVED rows belong in the pending reservation snapshot.")
        _decimal(self.notional, "notional", positive=True)
        for field in ("instrument_id", "idempotency_key", "sector"):
            value = getattr(self, field)
            if value is not None:
                _text(value, field)
        if self.currency is not None and re.fullmatch(r"[A-Z]{3}", self.currency) is None:
            raise PortfolioContractError("reservation currency must be an uppercase three-letter currency when provided.")
        if self.sector is not None:
            _text(self.sector, "sector")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))


def compute_reservation_snapshot_digest(
    source_id: str, revision: int, reservations: tuple[ReservationReference, ...]
) -> str:
    return hashlib.sha256(canonical_bytes({
        "source_id": source_id, "revision": revision,
        "reservations": tuple(sorted(reservations, key=lambda item: item.proposal_id)),
    })).hexdigest()


@dataclass(frozen=True, slots=True)
class ReservationSnapshot:
    """Read-only view supplied by orchestration; this package never owns reservations."""

    source_id: str
    revision: int
    reservations: tuple[ReservationReference, ...]
    digest: str

    def __post_init__(self) -> None:
        _text(self.source_id, "reservation source_id")
        if type(self.revision) is not int or self.revision < 0:
            raise PortfolioContractError("reservation revision must be a non-negative integer.")
        if not isinstance(self.reservations, tuple) or any(not isinstance(x, ReservationReference) for x in self.reservations):
            raise PortfolioContractError("reservations must be an immutable tuple of ReservationReference.")
        proposal_ids = [item.proposal_id for item in self.reservations]
        if len(proposal_ids) != len(set(proposal_ids)):
            raise PortfolioContractError("active reservation proposal IDs must be unique.")
        object.__setattr__(self, "reservations", tuple(sorted(self.reservations, key=lambda item: item.proposal_id)))
        _digest(self.digest, "reservation digest")
        if self.digest != compute_reservation_snapshot_digest(self.source_id, self.revision, self.reservations):
            raise PortfolioContractError("reservation digest does not match its contents.")


@dataclass(frozen=True, slots=True)
class PaperFillEvent:
    event_id: str
    sequence: int
    request_id: str
    proposal_id: str
    idempotency_key: str
    instrument_id: str
    symbol: str
    side: str
    quantity: Decimal
    fill_price: Decimal
    reference_price: Decimal
    fee: Decimal
    multiplier: Decimal
    currency: str
    market_snapshot_id: str
    occurred_at: datetime
    event_type: PortfolioEventType = PortfolioEventType.PAPER_FILL

    def __post_init__(self) -> None:
        for field in ("event_id", "request_id", "proposal_id", "idempotency_key", "instrument_id", "symbol", "market_snapshot_id"):
            _text(getattr(self, field), field)
        if type(self.sequence) is not int or self.sequence <= 0:
            raise PortfolioContractError("event sequence must be a positive integer.")
        if self.side not in ("BUY", "SELL"):
            raise PortfolioContractError("fill side must be BUY or SELL.")
        _decimal(self.quantity, "quantity", positive=True)
        _decimal(self.fill_price, "fill_price", positive=True)
        _decimal(self.reference_price, "reference_price", positive=True)
        _decimal(self.fee, "fee", nonnegative=True)
        _decimal(self.multiplier, "multiplier", positive=True)
        if not isinstance(self.currency, str) or re.fullmatch(r"[A-Z]{3}", self.currency) is None:
            raise PortfolioContractError("event currency must be an uppercase three-letter currency.")
        if self.event_type is not PortfolioEventType.PAPER_FILL:
            raise PortfolioContractError("fill event_type must be PAPER_FILL.")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))


@dataclass(frozen=True, slots=True)
class MarkToMarketEvent:
    event_id: str
    sequence: int
    instrument_id: str
    symbol: str
    price: Decimal
    currency: str
    multiplier: Decimal
    market_snapshot_id: str
    occurred_at: datetime
    event_type: PortfolioEventType = PortfolioEventType.MARK_TO_MARKET

    def __post_init__(self) -> None:
        for field in ("event_id", "instrument_id", "symbol", "market_snapshot_id"):
            _text(getattr(self, field), field)
        if type(self.sequence) is not int or self.sequence <= 0:
            raise PortfolioContractError("event sequence must be a positive integer.")
        _decimal(self.price, "price", positive=True)
        _decimal(self.multiplier, "multiplier", positive=True)
        if not isinstance(self.currency, str) or re.fullmatch(r"[A-Z]{3}", self.currency) is None:
            raise PortfolioContractError("event currency must be an uppercase three-letter currency.")
        if self.event_type is not PortfolioEventType.MARK_TO_MARKET:
            raise PortfolioContractError("mark event_type must be MARK_TO_MARKET.")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))


PortfolioEvent = PaperFillEvent | MarkToMarketEvent


@dataclass(frozen=True, slots=True)
class PortfolioPosition:
    instrument_id: str
    symbol: str
    quantity: Decimal
    cost_basis: Decimal
    market_value: Decimal
    mark_price: Decimal
    multiplier: Decimal
    currency: str
    market_snapshot_id: str
    sector: str | None = None

    def __post_init__(self) -> None:
        for field in ("instrument_id", "symbol", "market_snapshot_id"):
            _text(getattr(self, field), field)
        _decimal(self.quantity, "position.quantity", positive=True)
        _decimal(self.cost_basis, "position.cost_basis", nonnegative=True)
        _decimal(self.market_value, "position.market_value", positive=True)
        _decimal(self.mark_price, "position.mark_price", positive=True)
        _decimal(self.multiplier, "position.multiplier", positive=True)
        if not isinstance(self.currency, str) or re.fullmatch(r"[A-Z]{3}", self.currency) is None:
            raise PortfolioContractError("position currency must be an uppercase three-letter code.")
        if self.sector is not None:
            _text(self.sector, "position.sector")


@dataclass(frozen=True, slots=True)
class PortfolioOrderRecord:
    event_id: str
    request_id: str
    proposal_id: str
    idempotency_key: str
    instrument_id: str
    symbol: str
    side: str
    quantity: Decimal
    fill_price: Decimal
    fee: Decimal
    currency: str
    occurred_at: datetime
    market_snapshot_id: str

    def __post_init__(self) -> None:
        for field in ("event_id", "request_id", "proposal_id", "idempotency_key", "instrument_id", "symbol", "market_snapshot_id"):
            _text(getattr(self, field), field)
        if self.side not in ("BUY", "SELL"):
            raise PortfolioContractError("order side must be BUY or SELL.")
        _decimal(self.quantity, "order.quantity", positive=True)
        _decimal(self.fill_price, "order.fill_price", positive=True)
        _decimal(self.fee, "order.fee", nonnegative=True)
        if not isinstance(self.currency, str) or re.fullmatch(r"[A-Z]{3}", self.currency) is None:
            raise PortfolioContractError("order currency must be an uppercase three-letter code.")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "order.occurred_at"))


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    portfolio_id: str
    base_currency: str
    config_digest: str
    approval_record_id: str
    approval_record_digest: str
    as_of: datetime
    revision: int
    reservation_source_id: str
    reservation_revision: int
    reservation_digest: str
    initial_cash: Decimal
    cash: Decimal
    equity: Decimal
    peak_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    daily_pnl: Decimal
    positions: tuple[PortfolioPosition, ...]
    pending_exposures: tuple[ReservationReference, ...]
    order_history: tuple[PortfolioOrderRecord, ...]
    event_refs: tuple[str, ...]
    event_digests: tuple[str, ...]
    digest: str
    snapshot_id: str
    authority_status: str = "FIXTURE_PROJECTION_ONLY"

    def __post_init__(self) -> None:
        _text(self.portfolio_id, "portfolio_id")
        if not isinstance(self.base_currency, str) or re.fullmatch(r"[A-Z]{3}", self.base_currency) is None:
            raise PortfolioContractError("base_currency must be an uppercase three-letter currency.")
        _text(self.approval_record_id, "approval_record_id")
        _digest(self.config_digest, "config_digest")
        _digest(self.approval_record_digest, "approval_record_digest")
        _digest(self.reservation_digest, "reservation_digest")
        _digest(self.digest, "snapshot digest")
        if self.snapshot_id != f"paper-portfolio:{self.digest}":
            raise PortfolioContractError("snapshot_id must be derived from the snapshot digest.")
        if self.authority_status != "FIXTURE_PROJECTION_ONLY":
            raise PortfolioContractError("V0 portfolio projections cannot claim operational authority.")
        object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))
        if type(self.revision) is not int or self.revision < 0:
            raise PortfolioContractError("portfolio revision must be a non-negative integer.")
        if type(self.reservation_revision) is not int or self.reservation_revision < 0:
            raise PortfolioContractError("reservation revision must be a non-negative integer.")
        if not isinstance(self.positions, tuple) or any(not isinstance(x, PortfolioPosition) for x in self.positions):
            raise PortfolioContractError("positions must be an immutable tuple of PortfolioPosition.")
        if not isinstance(self.pending_exposures, tuple) or any(not isinstance(x, ReservationReference) for x in self.pending_exposures):
            raise PortfolioContractError("pending_exposures must be an immutable tuple of ReservationReference.")
        if not isinstance(self.order_history, tuple) or any(not isinstance(x, PortfolioOrderRecord) for x in self.order_history):
            raise PortfolioContractError("order_history must be an immutable tuple of PortfolioOrderRecord.")
        if (not isinstance(self.event_refs, tuple) or not isinstance(self.event_digests, tuple)
                or len(self.event_refs) != len(self.event_digests) or len(self.event_refs) != self.revision):
            raise PortfolioContractError("event references/digests must be immutable and match the snapshot revision.")
        for index, value in enumerate(self.event_digests):
            _digest(value, f"event_digests[{index}]")
        for name in ("cash", "equity", "peak_equity", "realized_pnl", "unrealized_pnl", "total_pnl", "daily_pnl"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise PortfolioContractError(f"{name} must be a finite Decimal.")
        _decimal(self.initial_cash, "initial_cash", nonnegative=True)
        if self.cash < 0:
            raise PortfolioContractError("cash cannot be negative in the V0 no-margin projection.")
        if len({item.instrument_id for item in self.positions}) != len(self.positions):
            raise PortfolioContractError("portfolio snapshot cannot contain duplicate position instruments.")
        if any(item.currency != self.base_currency for item in self.positions):
            raise PortfolioContractError("position currency must match portfolio base_currency.")
        if any(item.currency is not None and item.currency != self.base_currency for item in self.pending_exposures):
            raise PortfolioContractError("reservation currency must match portfolio base_currency when known.")
        if any(item.created_at > self.as_of for item in self.pending_exposures):
            raise PortfolioContractError("reservation cannot be later than snapshot as_of.")
        if self.reservation_digest != compute_reservation_snapshot_digest(
            self.reservation_source_id, self.reservation_revision, self.pending_exposures
        ):
            raise PortfolioContractError("snapshot reservation rows do not match the attached reservation digest.")
        if len(set(self.event_refs)) != len(self.event_refs):
            raise PortfolioContractError("snapshot event references must be unique.")
        if any(item.market_value != item.mark_price * item.quantity * item.multiplier for item in self.positions):
            raise PortfolioContractError("position market value does not reconcile to its mark and units.")
        if self.equity != self.cash + sum((item.market_value for item in self.positions), Decimal("0")):
            raise PortfolioContractError("equity must equal cash plus position market values.")
        if self.total_pnl != self.equity - self.initial_cash:
            raise PortfolioContractError("total_pnl must reconcile to equity minus initial_cash.")
        if self.unrealized_pnl != sum((item.market_value - item.cost_basis for item in self.positions), Decimal("0")):
            raise PortfolioContractError("unrealized_pnl must reconcile to marked positions and cost basis.")
        if self.total_pnl != self.realized_pnl + self.unrealized_pnl:
            raise PortfolioContractError("total_pnl must reconcile to realized and unrealized PnL.")
        if self.peak_equity < max(self.initial_cash, self.equity):
            raise PortfolioContractError("peak_equity cannot be below initial or current equity.")
        expected = hashlib.sha256(canonical_bytes({
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name not in {"digest", "snapshot_id"}
        })).hexdigest()
        if self.digest != expected:
            raise PortfolioContractError("portfolio snapshot digest does not match its contents.")

    def to_risk_state(self):
        """Create a Risk PortfolioState view; caller must still supply it authoritatively.

        This conversion does not verify the approval reference or make snapshot
        or reservation data transactionally current.
        """
        from invest_ai.risk.models import PendingExposure, PortfolioState, Position

        if any(item.instrument_id is None or item.currency != self.base_currency for item in self.pending_exposures):
            raise PortfolioContractError(
                "Risk mapping requires instrument_id and matching currency on every reservation; "
                "the current orchestration reservation source does not provide them."
            )

        return PortfolioState(
            snapshot_id=self.snapshot_id,
            as_of=self.as_of,
            cash=self.cash,
            equity=self.equity,
            peak_equity=self.peak_equity,
            daily_pnl=self.daily_pnl,
            base_currency=self.base_currency,
            positions=tuple(Position(x.instrument_id, x.quantity, x.market_value, x.sector) for x in self.positions),
            pending_exposures=tuple(PendingExposure(x.instrument_id, x.notional, x.sector) for x in self.pending_exposures),
            recent_order_times=tuple(x.occurred_at for x in self.order_history),
            processed_proposal_ids=frozenset(x.proposal_id for x in self.order_history),
            processed_idempotency_keys=frozenset(x.idempotency_key for x in self.order_history),
        )


__all__ = [
    "MarkToMarketEvent", "PaperFillEvent", "PortfolioBootstrapConfig", "PortfolioContractError",
    "PortfolioEvent", "PortfolioEventType", "PortfolioOrderRecord", "PortfolioPosition",
    "PortfolioSnapshot", "ReservationReference", "ReservationSnapshot", "ReservationStatus",
    "canonical_bytes", "compute_bootstrap_config_digest", "compute_reservation_snapshot_digest",
]
