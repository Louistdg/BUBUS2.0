"""Fail-closed, read-only valuation of Crypto.com Spot account observations."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

from invest_ai.data.snapshot_store import MarketSnapshotStore, SnapshotRecord, SnapshotStoreError
from invest_ai.portfolio.cryptocom_readonly import CryptoComPortfolioSnapshot
from invest_ai.portfolio.cryptocom_readonly import (
    CryptoComBalance, CryptoComOpenOrder, CryptoComPosition, CryptoComWalletBalance,
)


_PAIR = re.compile(r"([A-Z0-9][A-Z0-9.-]{0,15})_([A-Z]{3})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ZERO = Decimal("0")


class CryptoComCandidateError(ValueError):
    """Portfolio evidence cannot support a complete Spot valuation candidate."""


@dataclass(frozen=True, slots=True)
class CryptoComPortfolioCandidate:
    snapshot_id: str
    as_of: datetime
    base_currency: str
    total_equity: Decimal
    cash_value: Decimal
    spot_assets_value: Decimal
    held_asset_values: tuple[tuple[str, Decimal], ...]
    order_reservations: tuple[tuple[str, Decimal], ...]
    market_snapshot_ids: tuple[str, ...]
    source_payload_sha256: tuple[str, ...]
    authority_status: str = "BROKER_SPOT_PORTFOLIO_CANDIDATE_ONLY"
    usable_for_risk: bool = False
    usable_for_execution: bool = False
    orders_enabled: bool = False


def evaluate_spot_portfolio_candidate(
    snapshot: CryptoComPortfolioSnapshot,
    market_snapshot_ids: Mapping[str, str],
    snapshot_store: MarketSnapshotStore,
    *,
    base_currency: str,
    as_of: datetime,
    max_age_seconds: int = 30,
) -> CryptoComPortfolioCandidate:
    """Calculate a candidate value; never produce Risk authority or FX assumptions.

    Open BUY orders reserve quote currency at their limit price. Open SELL orders
    reserve the unfilled base quantity. Wallet balances include reserved assets,
    so those reserves are kept separate from held asset value and never added to
    equity a second time.
    """
    if not isinstance(snapshot, CryptoComPortfolioSnapshot):
        raise CryptoComCandidateError("A typed Crypto.com portfolio snapshot is required.")
    digests = (snapshot.payload_sha256, snapshot.positions_payload_sha256, snapshot.open_orders_payload_sha256)
    if any(not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in digests):
        raise CryptoComCandidateError("Crypto.com portfolio evidence hashes are incomplete.")
    if (not snapshot.balances or len({item.currency for item in snapshot.balances}) != len(snapshot.balances)
            or any(not isinstance(item.payload_sha256, str) or _SHA256.fullmatch(item.payload_sha256) is None
                   for item in snapshot.balances)
            or any(not isinstance(item.payload_sha256, str) or _SHA256.fullmatch(item.payload_sha256) is None
                   for item in snapshot.wallet_balances)):
        raise CryptoComCandidateError("Crypto.com balance evidence is incomplete or contradictory.")
    if not isinstance(base_currency, str) or re.fullmatch(r"[A-Z]{3}", base_currency) is None:
        raise CryptoComCandidateError("An explicit three-letter base currency is required.")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise CryptoComCandidateError("A timezone-aware valuation time is required.")
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300:
        raise CryptoComCandidateError("The portfolio freshness limit is invalid.")
    cutoff = as_of.astimezone(timezone.utc)
    if snapshot.fetched_at > cutoff or cutoff - snapshot.fetched_at > timedelta(seconds=max_age_seconds):
        raise CryptoComCandidateError("The Crypto.com portfolio observation is future-dated or stale.")
    if snapshot.positions:
        raise CryptoComCandidateError("Derivative or non-Spot positions are present; valuation is refused.")
    if not snapshot.balances:
        raise CryptoComCandidateError("The account has no complete balance rows.")
    for balance in snapshot.balances:
        if (balance.total_borrow != _ZERO or balance.isolated_cash != _ZERO or balance.is_liquidating
                or balance.has_isolated_positions):
            raise CryptoComCandidateError("Borrowing, isolated balances, or liquidation prevent a Spot valuation.")
        if (balance.initial_margin != _ZERO or balance.total_position_im is not None
                or balance.total_haircut is not None):
            if balance.total_position_im is None or balance.total_haircut is None:
                raise CryptoComCandidateError("Initial margin is not decomposed into position margin and haircut.")
            if (balance.total_position_im < _ZERO or balance.total_haircut < _ZERO
                    or balance.total_position_im + balance.total_haircut != balance.initial_margin):
                raise CryptoComCandidateError("Crypto.com initial-margin components do not reconcile.")
            if balance.total_position_im != _ZERO:
                raise CryptoComCandidateError("Open margin positions or orders prevent a Spot valuation.")
    if not isinstance(market_snapshot_ids, Mapping) or not isinstance(snapshot_store, MarketSnapshotStore):
        raise CryptoComCandidateError("A verified market snapshot store and references are required.")

    records: dict[str, SnapshotRecord] = {}
    record_ids: set[str] = set()
    for symbol, snapshot_id in market_snapshot_ids.items():
        if not isinstance(symbol, str) or not isinstance(snapshot_id, str):
            raise CryptoComCandidateError("Market snapshot mapping is invalid.")
        try:
            record = snapshot_store.get(snapshot_id)
        except SnapshotStoreError:
            raise CryptoComCandidateError("A referenced market snapshot failed integrity verification.") from None
        if not isinstance(record, SnapshotRecord):
            raise CryptoComCandidateError("A referenced market snapshot is invalid.")
        if record.snapshot.symbol != symbol or record.snapshot.currency != symbol.rsplit("_", 1)[-1]:
            raise CryptoComCandidateError("Market snapshot key does not match its verified instrument.")
        if record.snapshot.venue != "CRYPTOCOM" or record.observation_type != "Quote":
            raise CryptoComCandidateError("Only Crypto.com two-sided Spot quote snapshots are accepted.")
        try:
            assessment = snapshot_store.assess_for_risk(snapshot_id, as_of=cutoff,
                max_age=timedelta(seconds=max_age_seconds))
        except SnapshotStoreError:
            raise CryptoComCandidateError("A referenced market snapshot failed its Risk input assessment.") from None
        if not assessment.eligible:
            raise CryptoComCandidateError(f"Market quote {symbol} is not admissible: {', '.join(assessment.reasons)}.")
        if record.snapshot.observed_at > cutoff or record.snapshot.available_at > cutoff:
            raise CryptoComCandidateError("A market snapshot contains future data.")
        if cutoff - record.snapshot.observed_at > timedelta(seconds=max_age_seconds):
            raise CryptoComCandidateError(f"Market quote {symbol} is stale.")
        records[symbol] = record
        record_ids.add(record.snapshot_id)

    account_cash_by_currency: dict[str, Decimal] = {}
    for balance in snapshot.balances:
        account_cash_by_currency[balance.currency] = balance.total_cash
    wallet_by_asset: dict[str, Decimal] = {}
    wallet_account_currencies: set[str] = set()
    for balance in snapshot.wallet_balances:
        if balance.currency in wallet_by_asset:
            raise CryptoComCandidateError("Duplicate wallet asset rows prevent a complete valuation.")
        if balance.quantity < _ZERO or balance.reserved_quantity < _ZERO or balance.reserved_quantity > balance.quantity:
            raise CryptoComCandidateError("Wallet quantities or reserved quantities are inconsistent.")
        wallet_by_asset[balance.currency] = balance.quantity
        wallet_account_currencies.add(balance.account_currency)
        if balance.account_currency not in account_cash_by_currency:
            raise CryptoComCandidateError("Wallet balance references an unreported account currency.")

    held_values: dict[str, Decimal] = {}
    market_value_total = _ZERO
    if not wallet_by_asset:
        if any(amount != _ZERO for amount in account_cash_by_currency.values()):
            raise CryptoComCandidateError("Account cash is reported without itemized wallet balances.")
        raise CryptoComCandidateError("No itemized wallet balances are available for Spot valuation.")
    for asset, quantity in wallet_by_asset.items():
        if quantity == _ZERO:
            held_values[asset] = _ZERO
            continue
        if asset == base_currency:
            value = quantity
        else:
            price = _price_to_base(asset, base_currency, records)
            value = quantity * price
            record = _record_for(asset, base_currency, records)
            record_ids.add(record.snapshot_id)
        held_values[asset] = value
        market_value_total += value

    # total_cash_balance is an account-level wallet metric, not another asset
    # row. Itemized position_balances are valued once to avoid double counting.
    cash_value = held_values.get(base_currency, _ZERO)
    spot_assets_value = market_value_total - cash_value
    for currency, amount in account_cash_by_currency.items():
        if amount < _ZERO:
            raise CryptoComCandidateError("Negative account cash is outside the Spot candidate scope.")
        if amount != _ZERO and currency not in wallet_account_currencies:
            raise CryptoComCandidateError("Account cash lacks its itemized wallet asset rows.")

    reservations: list[tuple[str, Decimal]] = []
    sell_reservations: dict[str, Decimal] = {}
    for order in snapshot.open_orders:
        match = _PAIR.fullmatch(order.instrument_name)
        if match is None or order.order_type != "LIMIT" or order.status not in {"PENDING", "NEW", "ACTIVE"}:
            raise CryptoComCandidateError("An open order is not a supported Spot LIMIT order.")
        base_asset, quote_asset = match.groups()
        remaining = order.quantity - order.cumulative_quantity
        if remaining <= _ZERO:
            continue
        if order.side == "BUY":
            if order.limit_price is None:
                raise CryptoComCandidateError("An open BUY order has no limit price to reserve its exposure.")
            reserved_value = remaining * order.limit_price
            if quote_asset != base_currency:
                reserved_value *= _price_to_base(quote_asset, base_currency, records)
                record_ids.add(_record_for(quote_asset, base_currency, records).snapshot_id)
            reservations.append((order.order_id, reserved_value))
        else:
            if base_asset not in wallet_by_asset:
                raise CryptoComCandidateError("Open SELL order quantity cannot be reconciled to wallet holdings.")
            sell_reservations[base_asset] = sell_reservations.get(base_asset, _ZERO) + remaining
            value = remaining if base_asset == base_currency else remaining * _price_to_base(base_asset, base_currency, records)
            if base_asset != base_currency:
                record_ids.add(_record_for(base_asset, base_currency, records).snapshot_id)
            reservations.append((order.order_id, value))
    if any(quantity > wallet_by_asset.get(asset, _ZERO) for asset, quantity in sell_reservations.items()):
        raise CryptoComCandidateError("Combined open SELL orders exceed the corresponding wallet holding.")

    source_digests = {snapshot.payload_sha256, snapshot.positions_payload_sha256, snapshot.open_orders_payload_sha256}
    source_digests.update(item.payload_sha256 for item in snapshot.balances)
    source_digests.update(item.payload_sha256 for item in snapshot.wallet_balances)
    source_digests.discard("")
    digest = hashlib.sha256(json.dumps({
        "as_of": cutoff.isoformat(), "base_currency": base_currency,
        "cash": str(cash_value), "assets": sorted((key, str(value)) for key, value in held_values.items()),
        "reservations": sorted((key, str(value)) for key, value in reservations),
        "market_snapshot_ids": sorted(record_ids), "source_payload_sha256": sorted(source_digests),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return CryptoComPortfolioCandidate(
        snapshot_id=f"cryptocom-spot-candidate:{digest}", as_of=cutoff, base_currency=base_currency,
        total_equity=cash_value + spot_assets_value, cash_value=cash_value,
        spot_assets_value=spot_assets_value, held_asset_values=tuple(sorted(held_values.items())),
        order_reservations=tuple(sorted(reservations)), market_snapshot_ids=tuple(sorted(record_ids)),
        source_payload_sha256=tuple(sorted(source_digests)),
    )


def cryptocom_snapshot_from_persisted(payload: object) -> CryptoComPortfolioSnapshot:
    """Rebuild the typed broker observation from an integrity-checked local row."""
    if not isinstance(payload, dict) or payload.get("provider") != "Crypto.com Exchange":
        raise CryptoComCandidateError("The stored Crypto.com observation is invalid.")
    if (payload.get("persistent") is not True or payload.get("authority_status") != "OBSERVATION_ONLY"
            or payload.get("usable_for_risk") is not False or payload.get("usable_for_execution") is not False
            or payload.get("orders_enabled") is not False):
        raise CryptoComCandidateError("The stored Crypto.com observation has contradictory authority flags.")
    try:
        balances = tuple(CryptoComBalance(
            currency=item["currency"], available=Decimal(item["available"]),
            total_margin=Decimal(item["total_margin"]), initial_margin=Decimal(item["initial_margin"]),
            total_cash=Decimal(item["total_cash"]), total_borrow=Decimal(item["total_borrow"]),
            isolated_cash=Decimal(item["isolated_cash"]), is_liquidating=item["is_liquidating"],
            has_isolated_positions=item["has_isolated_positions"], source=item["source"],
            payload_sha256=item["payload_sha256"],
            total_position_im=(None if item.get("total_position_im") is None else Decimal(item["total_position_im"])),
            total_haircut=(None if item.get("total_haircut") is None else Decimal(item["total_haircut"])),
        ) for item in payload["balances"])
        wallets = tuple(CryptoComWalletBalance(
            currency=item["currency"], account_currency=item["account_currency"],
            quantity=Decimal(item["quantity"]), market_value=Decimal(item["market_value"]),
            collateral_amount=Decimal(item["collateral_amount"]),
            max_withdrawal_balance=Decimal(item["max_withdrawal_balance"]),
            reserved_quantity=Decimal(item["reserved_quantity"]), source=item["source"],
            payload_sha256=item["payload_sha256"],
        ) for item in payload["wallet_balances"])
        positions = tuple(CryptoComPosition(
            instrument_name=item["instrument_name"], position_type=item["type"],
            quantity=Decimal(item["quantity"]), cost_usd=Decimal(item["cost_usd"]),
            open_position_pnl_usd=Decimal(item["open_position_pnl_usd"]),
            session_pnl_usd=Decimal(item["session_pnl_usd"]),
            updated_at=_parse_time(item["updated_at"]), source=item["source"],
            payload_sha256=item["payload_sha256"],
        ) for item in payload["positions"])
        orders = tuple(CryptoComOpenOrder(
            account_id=item["account_id"], order_id=item["order_id"], client_oid=item["client_oid"],
            instrument_name=item["instrument_name"], side=item["side"], order_type=item["order_type"],
            status=item["status"], quantity=Decimal(item["quantity"]),
            cumulative_quantity=Decimal(item["cumulative_quantity"]),
            limit_price=None if item["limit_price"] is None else Decimal(item["limit_price"]),
            created_at=None if item["created_at"] is None else _parse_time(item["created_at"]),
            updated_at=None if item["updated_at"] is None else _parse_time(item["updated_at"]),
            source=item["source"], payload_sha256=item["payload_sha256"],
        ) for item in payload["open_orders"])
        snapshot = CryptoComPortfolioSnapshot(
            fetched_at=_parse_time(payload["fetched_at"]), balances=balances,
            payload_sha256=payload["payload_sha256"], positions=positions,
            positions_payload_sha256=payload["positions_payload_sha256"], wallet_balances=wallets,
            open_orders=orders, open_orders_payload_sha256=payload["open_orders_payload_sha256"],
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise CryptoComCandidateError("The stored Crypto.com observation is incomplete or malformed.") from None
    return snapshot


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone")
    return parsed.astimezone(timezone.utc)


def _record_for(asset: str, base_currency: str, records: Mapping[str, SnapshotRecord]) -> SnapshotRecord:
    direct = records.get(f"{asset}_{base_currency}")
    if direct is not None:
        return direct
    inverse = records.get(f"{base_currency}_{asset}")
    if inverse is not None and inverse.snapshot.price > 0:
        return inverse
    raise CryptoComCandidateError(f"No verified Crypto.com Spot quote values {asset} in {base_currency}.")


def _price_to_base(asset: str, base_currency: str, records: Mapping[str, SnapshotRecord]) -> Decimal:
    record = _record_for(asset, base_currency, records)
    if record.snapshot.symbol == f"{asset}_{base_currency}":
        return record.snapshot.price
    if record.snapshot.symbol == f"{base_currency}_{asset}":
        return Decimal("1") / record.snapshot.price
    raise CryptoComCandidateError("Market price direction does not match requested conversion.")


__all__ = ["CryptoComCandidateError", "CryptoComPortfolioCandidate", "cryptocom_snapshot_from_persisted",
           "evaluate_spot_portfolio_candidate"]
