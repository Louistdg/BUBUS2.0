"""Strict boundary for converting untrusted JSON into a typed proposal."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from .engine import MAX_DECIMAL_DIGITS, MAX_DECIMAL_EXPONENT
from .models import AssetType, Side, TradeProposal


class GateStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class GateResult:
    status: GateStatus
    proposal: TradeProposal | None
    reason_codes: tuple[str, ...]


MAX_INPUT_BYTES = 65_536
MAX_TEXT_LENGTH = 256
MAX_ID_LENGTH = 128
MAX_SYMBOL_LENGTH = 32
MAX_EVIDENCE_REFS = 64
# Clock-skew tolerance applies only to TradeProposal.created_at. Market data
# timestamps remain subject to strict point-in-time checks in their consumers.
# This tolerance neither relaxes those checks nor authorizes execution.
MAX_FUTURE_SKEW_SECONDS = 5
SUPPORTED_ORDER_TYPES = frozenset({"MARKET", "LIMIT"})
_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_SYMBOL_RE = re.compile(r"\A[A-Z0-9][A-Z0-9._/-]{0,31}\Z", re.ASCII)
_DECIMAL_RE = re.compile(r"\A-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z", re.ASCII)
_REQUIRED = frozenset(
    {
        "proposal_id", "idempotency_key", "instrument_id", "symbol",
        "asset_type", "side", "quantity", "strategy_id", "created_at",
        "market_snapshot_id", "portfolio_snapshot_id",
    }
)
_OPTIONAL = frozenset({"order_type", "limit_price", "stop_price", "evidence_refs"})


class _DuplicateKey(ValueError):
    pass


class _ForbiddenNumber(ValueError):
    pass


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise _ForbiddenNumber("NON_STANDARD_JSON_NUMBER")


def _reject_float(_: str) -> None:
    raise _ForbiddenNumber("JSON_FLOAT_FORBIDDEN")


def _invalid(*codes: str) -> GateResult:
    return GateResult(GateStatus.INVALID, None, tuple(dict.fromkeys(codes)))


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= MAX_ID_LENGTH and bool(_ID_RE.fullmatch(value))


def _decimal(value: Any) -> Decimal | None:
    # Financial values must be JSON strings. JSON floats (including integer
    # tokens) are deliberately not accepted, avoiding binary float ambiguity.
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT_LENGTH or not _DECIMAL_RE.fullmatch(value):
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    sign, digits, exponent = number.as_tuple()
    del sign
    if len(digits) > MAX_DECIMAL_DIGITS or abs(exponent) > MAX_DECIMAL_EXPONENT:
        return None
    return number


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def parse_trade_proposal(raw_json: str | bytes, *, now: datetime) -> GateResult:
    """Validate untrusted JSON and return a typed proposal or reason codes.

    This function only validates a proposal. It does not invoke the risk
    engine, select policy, contact a model/provider, or execute anything.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return _invalid("VALIDATION_TIME_INVALID")
    if not isinstance(raw_json, (str, bytes)):
        return _invalid("INPUT_TYPE_INVALID")
    try:
        encoded = raw_json.encode("utf-8") if isinstance(raw_json, str) else raw_json
    except UnicodeEncodeError:
        return _invalid("INPUT_ENCODING_INVALID")
    if len(encoded) > MAX_INPUT_BYTES:
        return _invalid("INPUT_TOO_LARGE")
    try:
        text = encoded.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_object_no_duplicates,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except _DuplicateKey:
        return _invalid("DUPLICATE_JSON_KEY")
    except _ForbiddenNumber as exc:
        return _invalid("JSON_FLOAT_FORBIDDEN" if str(exc) == "JSON_FLOAT_FORBIDDEN" else "NON_FINITE_JSON_NUMBER")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return _invalid("JSON_INVALID")
    if not isinstance(payload, dict):
        return _invalid("ROOT_OBJECT_REQUIRED")

    keys = frozenset(payload)
    unknown = keys - _REQUIRED - _OPTIONAL
    missing = _REQUIRED - keys
    if unknown:
        return _invalid("UNKNOWN_FIELDS")
    if missing:
        return _invalid("MISSING_FIELDS")

    codes: list[str] = []
    for name in ("proposal_id", "idempotency_key", "instrument_id", "strategy_id", "market_snapshot_id", "portfolio_snapshot_id"):
        if not _identifier(payload[name]):
            codes.append("IDENTIFIER_INVALID")
    symbol = payload["symbol"]
    if not isinstance(symbol, str) or len(symbol) > MAX_SYMBOL_LENGTH or not _SYMBOL_RE.fullmatch(symbol):
        codes.append("SYMBOL_INVALID")

    try:
        asset_type = AssetType(payload["asset_type"]) if isinstance(payload["asset_type"], str) else None
    except ValueError:
        asset_type = None
    if asset_type is None:
        codes.append("ASSET_TYPE_INVALID")
    try:
        side = Side(payload["side"]) if isinstance(payload["side"], str) else None
    except ValueError:
        side = None
    if side is None:
        codes.append("SIDE_INVALID")

    quantity = _decimal(payload["quantity"])
    if quantity is None:
        codes.append("QUANTITY_FORMAT_INVALID")
    elif quantity <= 0:
        codes.append("QUANTITY_NOT_POSITIVE")

    created_at = _timestamp(payload["created_at"])
    if created_at is None:
        codes.append("TIMESTAMP_INVALID_OR_NAIVE")
    elif (created_at - now).total_seconds() > MAX_FUTURE_SKEW_SECONDS:
        codes.append("TIMESTAMP_FUTURE")

    order_type = payload.get("order_type", "MARKET")
    if not isinstance(order_type, str) or len(order_type) > 32:
        codes.append("ORDER_TYPE_INVALID")
    elif order_type not in SUPPORTED_ORDER_TYPES:
        return GateResult(GateStatus.UNSUPPORTED, None, ("ORDER_TYPE_UNSUPPORTED",))

    limit_price = None
    if "limit_price" in payload and payload["limit_price"] is not None:
        limit_price = _decimal(payload["limit_price"])
        if limit_price is None:
            codes.append("LIMIT_PRICE_FORMAT_INVALID")
        elif limit_price <= 0:
            codes.append("LIMIT_PRICE_NOT_POSITIVE")
    if order_type == "LIMIT" and limit_price is None:
        codes.append("LIMIT_PRICE_REQUIRED")
    if order_type == "MARKET" and limit_price is not None:
        codes.append("LIMIT_PRICE_NOT_ALLOWED")
    stop_price = None
    if "stop_price" in payload and payload["stop_price"] is not None:
        stop_price = _decimal(payload["stop_price"])
        if stop_price is None:
            codes.append("STOP_PRICE_FORMAT_INVALID")
        elif stop_price <= 0:
            codes.append("STOP_PRICE_NOT_POSITIVE")

    evidence = payload.get("evidence_refs", [])
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE_REFS:
        codes.append("EVIDENCE_FORMAT_INVALID")
    elif any(not _identifier(item) for item in evidence):
        codes.append("EVIDENCE_FORMAT_INVALID")
    elif len(set(evidence)) != len(evidence):
        codes.append("EVIDENCE_DUPLICATE")

    if codes:
        return _invalid(*codes)

    proposal = TradeProposal(
        proposal_id=payload["proposal_id"],
        idempotency_key=payload["idempotency_key"],
        instrument_id=payload["instrument_id"],
        symbol=symbol,
        asset_type=asset_type,
        side=side,
        quantity=quantity,
        strategy_id=payload["strategy_id"],
        created_at=created_at,
        order_type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
        evidence_refs=tuple(evidence),
        market_snapshot_id=payload["market_snapshot_id"],
        portfolio_snapshot_id=payload["portfolio_snapshot_id"],
    )
    return GateResult(GateStatus.VALID, proposal, ())
