"""Strict, side-effect-free parser for IBKR Web API top-of-book stream messages.

This module does not open a connection, retain payloads, identify an account,
or authorize orders. Parsed updates are display/data-boundary inputs only.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Mapping


IBKR_TOP_OF_BOOK_FIELDS = ("31", "84", "85", "86", "88", "6509")
_CONID = re.compile(r"[1-9][0-9]{0,14}\Z")
_MILLIS = re.compile(r"[0-9]{13}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MESSAGE_BYTES = 64 * 1024


class IBKRStreamContractError(ValueError):
    """An IBKR stream message cannot safely be interpreted as a quote update."""


class IBKRMarketDataStatus(StrEnum):
    REALTIME = "REALTIME"
    DELAYED = "DELAYED"
    FROZEN = "FROZEN"
    FROZEN_DELAYED = "FROZEN_DELAYED"
    NOT_SUBSCRIBED = "NOT_SUBSCRIBED"
    INCOMPLETE = "INCOMPLETE"
    VDR_EXEMPT_TIME_UNKNOWN = "VDR_EXEMPT_TIME_UNKNOWN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class IBKRMarketUpdate:
    conid: int
    status: IBKRMarketDataStatus
    observed_at: datetime
    received_at: datetime
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    payload_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.conid, bool) or not isinstance(self.conid, int) or not _CONID.fullmatch(str(self.conid)):
            raise IBKRStreamContractError("conid must be a positive integer")
        if not isinstance(self.status, IBKRMarketDataStatus):
            raise IBKRStreamContractError("status must be canonical")
        for name in ("observed_at", "received_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise IBKRStreamContractError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        for name in ("bid", "ask", "last", "bid_size", "ask_size"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, Decimal) or not value.is_finite():
                    raise IBKRStreamContractError(f"{name} must be a finite Decimal")
                if value < 0 or (name in {"bid", "ask", "last"} and value == 0):
                    raise IBKRStreamContractError(f"{name} is outside its valid range")
        if not isinstance(self.payload_sha256, str) or not _SHA256.fullmatch(self.payload_sha256):
            raise IBKRStreamContractError("payload_sha256 must be lowercase SHA-256")

    @property
    def has_two_sided_quote(self) -> bool:
        return self.bid is not None and self.ask is not None

    @property
    def is_crossed(self) -> bool:
        return self.has_two_sided_quote and self.bid > self.ask

    def is_fresh(self, *, now: datetime, max_age: timedelta) -> bool:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise IBKRStreamContractError("now must be timezone-aware")
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise IBKRStreamContractError("max_age must be a positive timedelta")
        instant = now.astimezone(timezone.utc)
        observed = self.observed_at.astimezone(timezone.utc)
        return observed <= instant and observed >= instant - max_age

    def is_usable_realtime_quote(self, *, now: datetime, max_age: timedelta) -> bool:
        """A data-quality result only; it is not an order authorization."""
        return (self.status is IBKRMarketDataStatus.REALTIME
                and self.has_two_sided_quote and not self.is_crossed
                and self.is_fresh(now=now, max_age=max_age))


def _decimal_field(message: Mapping[str, object], field: str, *, nonnegative: bool = False) -> Decimal | None:
    value = message.get(field)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 50:
        raise IBKRStreamContractError(f"field {field} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise IBKRStreamContractError(f"field {field} is not a decimal") from exc
    if (not result.is_finite() or len(result.as_tuple().digits) > 40
            or abs(result.as_tuple().exponent) > 20):
        raise IBKRStreamContractError(f"field {field} exceeds numeric bounds")
    if result < 0 or (not nonnegative and result == 0):
        raise IBKRStreamContractError(f"field {field} must be {'nonnegative' if nonnegative else 'positive'}")
    return result


def _market_status(value: object) -> IBKRMarketDataStatus:
    if not isinstance(value, str) or not value:
        return IBKRMarketDataStatus.UNKNOWN
    return {
        "R": IBKRMarketDataStatus.REALTIME,
        "D": IBKRMarketDataStatus.DELAYED,
        "Z": IBKRMarketDataStatus.FROZEN,
        "Y": IBKRMarketDataStatus.FROZEN_DELAYED,
        "N": IBKRMarketDataStatus.NOT_SUBSCRIBED,
        "i": IBKRMarketDataStatus.INCOMPLETE,
        "v": IBKRMarketDataStatus.VDR_EXEMPT_TIME_UNKNOWN,
    }.get(value[0], IBKRMarketDataStatus.UNKNOWN)


def build_subscription_message(conid: int) -> str:
    """Return the documented subscription frame for live top-of-book fields."""
    if isinstance(conid, bool) or not isinstance(conid, int) or not _CONID.fullmatch(str(conid)):
        raise IBKRStreamContractError("conid must be a positive integer")
    text = str(conid)
    fields = json.dumps(list(IBKR_TOP_OF_BOOK_FIELDS), separators=(",", ":"))
    return f"smd+{text}+{{\"fields\":{fields}}}"


def build_unsubscription_message(conid: int) -> str:
    if isinstance(conid, bool) or not isinstance(conid, int) or not _CONID.fullmatch(str(conid)):
        raise IBKRStreamContractError("conid must be a positive integer")
    text = str(conid)
    return f"umd+{text}+{{}}"


def parse_market_data_message(
    payload: str | bytes | Mapping[str, object],
    *,
    expected_conid: int,
    received_at: datetime,
) -> IBKRMarketUpdate:
    """Parse one TWS/Web API message without inferring a missing status or time."""
    if (isinstance(expected_conid, bool) or not isinstance(expected_conid, int)
            or not _CONID.fullmatch(str(expected_conid))):
        raise IBKRStreamContractError("expected_conid must be a positive integer")
    conid_text = str(expected_conid)
    if not isinstance(received_at, datetime) or received_at.tzinfo is None or received_at.utcoffset() is None:
        raise IBKRStreamContractError("received_at must be timezone-aware")
    received = received_at.astimezone(timezone.utc)

    if isinstance(payload, bytes):
        raw = payload
        if len(raw) > _MAX_MESSAGE_BYTES:
            raise IBKRStreamContractError("payload exceeds the stream message size limit")
        try:
            message = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise IBKRStreamContractError("payload is not valid UTF-8 JSON") from exc
    elif isinstance(payload, str):
        try:
            raw = payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise IBKRStreamContractError("payload is not valid UTF-8 text") from exc
        if len(raw) > _MAX_MESSAGE_BYTES:
            raise IBKRStreamContractError("payload exceeds the stream message size limit")
        try:
            message = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise IBKRStreamContractError("payload is not valid JSON") from exc
    elif isinstance(payload, Mapping):
        message = dict(payload)
        try:
            raw = json.dumps(message, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise IBKRStreamContractError("mapping contains values that are not valid JSON") from exc
        if len(raw) > _MAX_MESSAGE_BYTES:
            raise IBKRStreamContractError("payload exceeds the stream message size limit")
    else:
        raise IBKRStreamContractError("payload must be JSON text or a mapping")

    if not isinstance(message, dict):
        raise IBKRStreamContractError("market data message must be a JSON object")
    actual_conid = message.get("conid")
    if isinstance(actual_conid, bool) or str(actual_conid) != conid_text:
        raise IBKRStreamContractError("message conid does not match the requested instrument")
    topic = message.get("topic")
    if topic is not None and topic != f"smd+{conid_text}":
        raise IBKRStreamContractError("message topic does not match the requested instrument")

    updated = message.get("_updated")
    if isinstance(updated, bool) or not _MILLIS.fullmatch(str(updated)):
        raise IBKRStreamContractError("_updated must be a 13-digit Unix timestamp in milliseconds")
    epoch_millis = int(str(updated))
    try:
        observed = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=epoch_millis)
    except OverflowError as exc:
        raise IBKRStreamContractError("_updated is outside the supported timestamp range") from exc
    if observed > received + timedelta(seconds=5):
        raise IBKRStreamContractError("market update timestamp is materially in the future")

    status = _market_status(message.get("6509"))
    return IBKRMarketUpdate(
        conid=int(conid_text),
        status=status,
        observed_at=observed,
        received_at=received,
        bid=_decimal_field(message, "84"),
        ask=_decimal_field(message, "86"),
        last=_decimal_field(message, "31"),
        bid_size=_decimal_field(message, "88", nonnegative=True),
        ask_size=_decimal_field(message, "85", nonnegative=True),
        payload_sha256=hashlib.sha256(raw).hexdigest(),
    )


__all__ = [
    "IBKRMarketDataStatus", "IBKRMarketUpdate", "IBKRStreamContractError",
    "IBKR_TOP_OF_BOOK_FIELDS", "build_subscription_message",
    "build_unsubscription_message", "parse_market_data_message",
]
