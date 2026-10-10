"""Exact arithmetic checks for explicitly evidenced per-asset journal postings.

This is a reusable reconciliation primitive, NOT a mapping from Crypto.com's
current transaction response and NOT a P&L or Risk authority. A complete
asset-level signed ledger is not yet available from the local observations.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Mapping

_ASSET = re.compile(r"[A-Z0-9]{2,16}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_KINDS = {"DEPOSIT", "WITHDRAWAL", "CONVERSION", "TRADE", "FEE"}
_REQUIRED = {"event_id", "leg_id", "kind", "asset", "delta", "source_sha256", "external_reference"}


class AssetLedgerError(ValueError):
    """The asset reconciliation inputs are malformed or untrusted."""


def spot_trade_postings(trades: object) -> list[dict]:
    """Project validated Crypto.com Spot fills into signed per-asset candidates.

    This only maps a complete, validated ``private/get-trades`` item into
    quantity deltas. It does not prove page completeness, link every journal
    event, value assets, calculate P&L, or grant Risk/execution authority.
    """
    if not isinstance(trades, list) or len(trades) > 10_000:
        raise AssetLedgerError("Spot trade collection is invalid.")
    result = []
    seen: set[tuple[str, str]] = set()
    for trade in trades:
        required = {"trade_id", "instrument_name", "side", "quantity", "price",
                    "fees", "fee_currency", "source", "payload_sha256"}
        if not isinstance(trade, dict) or set(trade) != required:
            raise AssetLedgerError("Spot trade evidence has an unexpected shape.")
        instrument = trade["instrument_name"]
        match = re.fullmatch(r"([A-Z0-9]{2,16})_([A-Z0-9]{2,16})", instrument) \
            if isinstance(instrument, str) else None
        trade_id = trade["trade_id"]
        if (match is None or match.group(1) == match.group(2)
                or not isinstance(trade_id, str) or not re.fullmatch(r"[0-9]{1,32}", trade_id)
                or not isinstance(trade["side"], str) or trade["side"] not in {"BUY", "SELL"}
                or trade["source"] != "private/get-trades"
                or not isinstance(trade["payload_sha256"], str)
                or _HASH.fullmatch(trade["payload_sha256"]) is None
                or not isinstance(trade["fee_currency"], str)
                or _ASSET.fullmatch(trade["fee_currency"]) is None):
            raise AssetLedgerError("Spot trade identity, pair, or provenance is invalid.")
        identity = (instrument, trade_id)
        if identity in seen:
            raise AssetLedgerError("Duplicate Spot trade; refusing to double count it.")
        seen.add(identity)
        quantity = _amount(trade["quantity"], allow_negative=False)
        price = _amount(trade["price"], allow_negative=False)
        fee = _amount(trade["fees"], allow_negative=True)
        if quantity <= 0 or price <= 0 or fee > 0:
            raise AssetLedgerError("Spot trade quantity/price or fee sign is invalid.")
        base_asset, quote_asset = match.groups()
        event_id = f"spot-trade:{instrument}:{trade_id}"
        base_delta = quantity if trade["side"] == "BUY" else -quantity
        quote_delta = -(quantity * price) if trade["side"] == "BUY" else quantity * price
        common = {"event_id": event_id, "kind": "TRADE",
                  "source_sha256": trade["payload_sha256"], "external_reference": None}
        result.extend([
            {**common, "leg_id": "base", "asset": base_asset, "delta": format(base_delta, "f")},
            {**common, "leg_id": "quote", "asset": quote_asset, "delta": format(quote_delta, "f")},
        ])
        if fee < 0:
            result.append({**common, "leg_id": "fee", "kind": "FEE",
                           "asset": trade["fee_currency"], "delta": format(fee, "f")})
    return result


def _amount(value: object, *, allow_negative: bool) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise AssetLedgerError("Ledger amount must be an explicit decimal string.")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise AssetLedgerError("Ledger amount is not a decimal.") from None
    if not amount.is_finite() or (not allow_negative and amount < 0):
        raise AssetLedgerError("Ledger amount is non-finite or outside the allowed range.")
    return amount


def _balances(value: object) -> dict[str, Decimal] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or len(value) > 500:
        raise AssetLedgerError("Asset balance mapping is invalid.")
    result = {}
    for asset, raw in value.items():
        if not isinstance(asset, str) or _ASSET.fullmatch(asset) is None:
            raise AssetLedgerError("Asset symbol is invalid.")
        result[asset] = _amount(raw, allow_negative=False)
    return result


def reconcile_asset_quantities(*, opening: Mapping[str, str] | None,
                               closing: Mapping[str, str] | None,
                               postings: list[dict] | None) -> dict:
    """Check opening + signed deltas = closing; never infer missing zero balances.

    MATCHED_ARITHMETIC_ONLY means the supplied postings add up, not that they
    are complete, authentic, tax/P&L accurate, or usable for Risk or execution.
    """
    start, end = _balances(opening), _balances(closing)
    if postings is not None and (not isinstance(postings, list) or len(postings) > 10_000):
        raise AssetLedgerError("Asset posting list is invalid.")

    reasons = []
    if start is None or end is None:
        reasons.append("OPENING_OR_CLOSING_MISSING")
    if postings is None:
        reasons.append("POSTINGS_MISSING")

    totals: dict[str, Decimal] = defaultdict(Decimal)
    grouped: dict[tuple[str, str], list[tuple[str, Decimal]]] = defaultdict(list)
    seen = set()
    for entry in postings or []:
        if not isinstance(entry, dict) or set(entry) != _REQUIRED:
            raise AssetLedgerError("Asset posting has missing or unexpected fields.")
        event_id, leg_id, kind, asset = (entry["event_id"], entry["leg_id"],
                                         entry["kind"], entry["asset"])
        if (not isinstance(event_id, str) or not 1 <= len(event_id) <= 128
                or not isinstance(leg_id, str) or not 1 <= len(leg_id) <= 128
                or kind not in _KINDS or not isinstance(asset, str) or _ASSET.fullmatch(asset) is None
                or not isinstance(entry["source_sha256"], str)
                or _HASH.fullmatch(entry["source_sha256"]) is None):
            raise AssetLedgerError("Asset posting identity or provenance is invalid.")
        identity = (event_id, leg_id)
        if identity in seen:
            raise AssetLedgerError("Duplicate posting leg; replay would double count.")
        seen.add(identity)
        delta = _amount(entry["delta"], allow_negative=True)
        external = entry["external_reference"]
        if external is not None and (not isinstance(external, str) or not 1 <= len(external) <= 128):
            raise AssetLedgerError("External transfer reference is invalid.")
        if kind in {"DEPOSIT", "WITHDRAWAL"}:
            if not external:
                reasons.append("EXTERNAL_TRANSFER_REFERENCE_MISSING")
            if (kind == "DEPOSIT" and delta <= 0) or (kind == "WITHDRAWAL" and delta >= 0):
                raise AssetLedgerError("External flow sign is inconsistent with its kind.")
        elif external is not None:
            raise AssetLedgerError("External transfer reference on a non-transfer posting.")
        if kind == "FEE" and delta >= 0:
            raise AssetLedgerError("Fees must be an explicit debit, not an estimated amount.")
        totals[asset] += delta
        grouped[(event_id, kind)].append((asset, delta))

    for (_, kind), legs in grouped.items():
        if kind in {"CONVERSION", "TRADE"}:
            if (len(legs) < 2 or len({asset for asset, _ in legs}) < 2
                    or not any(delta < 0 for _, delta in legs)
                    or not any(delta > 0 for _, delta in legs)):
                reasons.append("CONVERSION_OR_TRADE_LEG_MISSING")

    residuals = {}
    if start is not None and end is not None:
        for asset in set(start) | set(end) | set(totals):
            if asset not in start or asset not in end:
                reasons.append("ASSET_OPENING_OR_CLOSING_MISSING")
                continue
            residual = start[asset] + totals.get(asset, Decimal(0)) - end[asset]
            if residual:
                residuals[asset] = format(residual, "f")

    if residuals:
        reasons.append("QUANTITY_MISMATCH")
    status = ("BLOCKED_CONTRADICTORY" if residuals
              else "BLOCKED_INCOMPLETE" if reasons else "MATCHED_ARITHMETIC_ONLY")
    # Only publish record counts and discrepancies; never infer profit, fees
    # in a base currency, or fiat value from raw quantity differences.
    report = {
        "status": status, "reasons": list(dict.fromkeys(reasons)),
        "opening_assets": 0 if start is None else len(start),
        "closing_assets": 0 if end is None else len(end),
        "posting_count": 0 if postings is None else len(postings),
        "residual_quantities": residuals,
        "authority_status": "LOCAL_ARITHMETIC_OBSERVATION_ONLY",
        "provider_history_completeness_verified": False,
        "usable_for_pnl": False, "usable_for_risk": False,
        "usable_for_execution": False, "orders_enabled": False, "pnl": None,
    }
    report["evidence_sha256"] = hashlib.sha256(json.dumps({
        "opening": None if start is None else {k: str(v) for k, v in sorted(start.items())},
        "closing": None if end is None else {k: str(v) for k, v in sorted(end.items())},
        "postings": postings,
        "result": report,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return report


__all__ = ["AssetLedgerError", "reconcile_asset_quantities", "spot_trade_postings"]
