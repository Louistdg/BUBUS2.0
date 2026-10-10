"""Read-only IBKR Client Portal portfolio observations.

This adapter deliberately returns a broker observation, not the canonical
PAPER PortfolioSnapshot consumed by Risk. It performs GET requests only and
does not persist account data.
"""
from __future__ import annotations

import hashlib
import json
import re
import ssl
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping, Protocol

from invest_ai.data.ibkr_client import (
    IBKRClientPortalError,
    IBKRClientPortalSession,
)
from invest_ai.data.models import as_utc


_BASE_URL = "https://localhost:5000/v1/api"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_ALLOWED_PATH = re.compile(
    r"(?:/portfolio/accounts|/portfolio/[A-Za-z0-9_-]{1,32}/summary|"
    r"/portfolio2/[A-Za-z0-9_-]{1,32}/positions)\Z"
)


class IBKRPortfolioError(ValueError):
    """The requested read-only portfolio snapshot is invalid or incomplete."""


class JsonTransport(Protocol):
    def get_json(self, path: str) -> tuple[object, bytes]: ...


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class IBKRLocalGatewayTransport:
    """Pinned, verified-TLS HTTP transport to the local Client Portal Gateway."""

    def __init__(self, ca_file: str | Path, *, session: IBKRClientPortalSession | None = None) -> None:
        path = Path(ca_file).expanduser().resolve()
        if not path.is_file():
            raise IBKRPortfolioError("A trusted local Gateway certificate bundle is required.")
        self._ca_file = path
        self._session = session or IBKRClientPortalSession(path)

    def get_json(self, path: str) -> tuple[object, bytes]:
        if not isinstance(path, str) or not _ALLOWED_PATH.fullmatch(path):
            raise IBKRPortfolioError("Invalid local Gateway path.")
        token = self._session.tickle()
        request = urllib.request.Request(
            f"{_BASE_URL}{path}",
            headers={"Accept": "application/json", "Cookie": f"api={token}"},
            method="GET",
        )
        try:
            context = ssl.create_default_context(cafile=str(self._ca_file))
            if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                raise IBKRPortfolioError("Gateway TLS certificate and hostname verification must remain enabled.")
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
                _RejectRedirectHandler(),
            )
            response = opener.open(request, timeout=5)
            with response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except IBKRPortfolioError:
            raise
        except Exception:
            # Never include URL headers, cookies or provider bodies in an error.
            raise IBKRClientPortalError("Local Client Portal Gateway request failed or TLS verification failed.") from None
        if len(body) > _MAX_RESPONSE_BYTES:
            raise IBKRPortfolioError("Gateway response exceeded the configured size limit.")
        try:
            payload = json.loads(body, parse_float=Decimal, parse_int=int, parse_constant=_reject_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise IBKRPortfolioError("Gateway returned invalid JSON or a non-finite number.") from None
        return payload, body


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON number: {value}")


def _text(value: object, name: str, *, max_length: int = 128) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise IBKRPortfolioError(f"Invalid {name} in Gateway response.")
    return value.strip()


def _decimal(value: object, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise IBKRPortfolioError(f"Invalid decimal {name} in Gateway response.")
    try:
        number = value if isinstance(value, Decimal) else Decimal(value)  # type: ignore[arg-type]
    except (InvalidOperation, TypeError, ValueError):
        raise IBKRPortfolioError(f"Invalid decimal {name} in Gateway response.") from None
    if not number.is_finite():
        raise IBKRPortfolioError(f"Non-finite decimal {name} in Gateway response.")
    return number


def _currency(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise IBKRPortfolioError(f"Invalid {name} currency in Gateway response.")
    return value


def _epoch(value: object, name: str) -> datetime:
    raw = _decimal(value, name)
    assert raw is not None
    # IBKR's APIs use epoch seconds in positions and milliseconds in summaries.
    seconds = raw / Decimal("1000") if abs(raw) >= Decimal("100000000000") else raw
    try:
        micros = int((seconds * Decimal("1000000")).to_integral_value())
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micros)
    except (OverflowError, OSError, ValueError):
        raise IBKRPortfolioError(f"Invalid {name} timestamp in Gateway response.") from None


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True, slots=True)
class IBKRPortfolioAccount:
    account_id: str
    display_name: str
    currency: str | None
    provider: str = "IBKR"
    source: str = "Client Portal Gateway /portfolio/accounts"


@dataclass(frozen=True, slots=True)
class IBKRAccountMetric:
    name: str
    amount: Decimal
    currency: str
    provider_timestamp: datetime
    source: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class IBKRPositionObservation:
    account_id: str
    conid: int
    symbol: str
    asset_class: str
    quantity: Decimal
    currency: str
    average_price: Decimal | None
    market_price: Decimal | None
    market_value: Decimal | None
    provider_timestamp: datetime
    source: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class IBKRReadOnlyPortfolioSnapshot:
    account: IBKRPortfolioAccount
    fetched_at: datetime
    metrics: tuple[IBKRAccountMetric, ...]
    positions: tuple[IBKRPositionObservation, ...]
    accounts_payload_sha256: str
    summary_payload_sha256: str
    positions_payload_sha256: str
    authority_status: str = "BROKER_READ_ONLY_OBSERVATION_ONLY"
    persistent: bool = False
    usable_for_risk: bool = False
    usable_for_execution: bool = False


class IBKRReadOnlyPortfolioClient:
    """Discover accounts and read one explicitly selected account snapshot."""

    def __init__(self, transport: JsonTransport, *, clock: Callable[[], datetime] | None = None,
                 max_snapshot_age: timedelta = timedelta(seconds=60),
                 max_snapshot_skew: timedelta = timedelta(seconds=60)) -> None:
        if not isinstance(max_snapshot_age, timedelta) or max_snapshot_age <= timedelta(0):
            raise ValueError("max_snapshot_age must be a positive timedelta")
        if not isinstance(max_snapshot_skew, timedelta) or max_snapshot_skew <= timedelta(0):
            raise ValueError("max_snapshot_skew must be a positive timedelta")
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_age = max_snapshot_age
        self._max_skew = max_snapshot_skew

    def list_accounts(self) -> tuple[IBKRPortfolioAccount, ...]:
        payload, _body = self._transport.get_json("/portfolio/accounts")
        return _parse_accounts(payload)

    def read_portfolio(self, account_id: str) -> IBKRReadOnlyPortfolioSnapshot:
        requested_id = _text(account_id, "selected account ID", max_length=32)
        if not _ACCOUNT_ID.fullmatch(requested_id):
            raise IBKRPortfolioError("Invalid selected account ID.")

        accounts_payload, accounts_body = self._transport.get_json("/portfolio/accounts")
        accounts = _parse_accounts(accounts_payload)
        matches = [account for account in accounts if account.account_id == requested_id]
        if len(matches) != 1:
            raise IBKRPortfolioError("Selected account is absent or ambiguous in the authenticated account list.")
        account = matches[0]

        summary_payload, summary_body = self._transport.get_json(f"/portfolio/{account.account_id}/summary")
        positions_payload, positions_body = self._transport.get_json(f"/portfolio2/{account.account_id}/positions")
        fetched_at = as_utc(self._clock(), "fetched_at")
        metrics = _parse_summary(summary_payload, _digest(summary_body), fetched_at, self._max_age)
        positions = _parse_positions(
            positions_payload, account.account_id, _digest(positions_body), fetched_at, self._max_age,
        )

        timestamps = [metric.provider_timestamp for metric in metrics]
        timestamps.extend(position.provider_timestamp for position in positions)
        if timestamps and max(timestamps) - min(timestamps) > self._max_skew:
            raise IBKRPortfolioError("Account metrics and positions exceed the allowed snapshot time skew.")
        return IBKRReadOnlyPortfolioSnapshot(
            account=account,
            fetched_at=fetched_at,
            metrics=metrics,
            positions=positions,
            accounts_payload_sha256=_digest(accounts_body),
            summary_payload_sha256=_digest(summary_body),
            positions_payload_sha256=_digest(positions_body),
        )


def _parse_accounts(payload: object) -> tuple[IBKRPortfolioAccount, ...]:
    if not isinstance(payload, list):
        raise IBKRPortfolioError("Gateway account list must be a JSON array.")
    result: list[IBKRPortfolioAccount] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            raise IBKRPortfolioError("Gateway account entry must be an object.")
        account_id = _text(item.get("accountId", item.get("id")), "account ID", max_length=32)
        if not _ACCOUNT_ID.fullmatch(account_id) or account_id in seen:
            raise IBKRPortfolioError("Gateway account IDs must be unique and valid.")
        seen.add(account_id)
        display = item.get("displayName", item.get("accountAlias", account_id))
        if display is None or not isinstance(display, str):
            display = account_id
        currency = _currency(item.get("currency"), "account", optional=True)
        result.append(IBKRPortfolioAccount(account_id, display[:128], currency))
    if not result:
        raise IBKRPortfolioError("No readable accounts were returned by the authenticated Gateway.")
    return tuple(result)


def _parse_summary(payload: object, digest: str, fetched_at: datetime,
                   max_age: timedelta) -> tuple[IBKRAccountMetric, ...]:
    if not isinstance(payload, Mapping):
        raise IBKRPortfolioError("Gateway account summary must be a JSON object.")
    result: list[IBKRAccountMetric] = []
    for key, item in payload.items():
        if not isinstance(key, str) or not isinstance(item, Mapping) or "amount" not in item:
            continue
        amount = _decimal(item.get("amount"), f"summary.{key}.amount", optional=True)
        currency = _currency(item.get("currency"), f"summary.{key}", optional=True)
        # Skip non-monetary fields. Monetary values without currency are unsafe.
        if amount is None or currency is None:
            continue
        timestamp = _epoch(item.get("timestamp"), f"summary.{key}.timestamp")
        if timestamp > fetched_at or fetched_at - timestamp > max_age:
            raise IBKRPortfolioError(f"Account metric {key} is future-dated or stale.")
        result.append(IBKRAccountMetric(
            name=key, amount=amount, currency=currency, provider_timestamp=timestamp,
            source=f"Client Portal Gateway /portfolio/{{accountId}}/summary#{key}",
            payload_sha256=digest,
        ))
    if not result:
        raise IBKRPortfolioError("Gateway summary contains no timestamped monetary metrics with currencies.")
    return tuple(result)


def _parse_positions(payload: object, account_id: str, digest: str, fetched_at: datetime,
                     max_age: timedelta) -> tuple[IBKRPositionObservation, ...]:
    if not isinstance(payload, list):
        raise IBKRPortfolioError("Gateway positions response must be a JSON array.")
    result: list[IBKRPositionObservation] = []
    seen: set[int] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            raise IBKRPortfolioError("Gateway position entry must be an object.")
        row_account = item.get("acctId", item.get("accountId", account_id))
        if row_account != account_id:
            raise IBKRPortfolioError("Position account ID does not match the explicitly selected account.")
        conid_value = item.get("conid")
        if type(conid_value) is not int or conid_value <= 0 or conid_value in seen:
            raise IBKRPortfolioError("Position contract IDs must be unique positive integers.")
        seen.add(conid_value)
        quantity = _decimal(item.get("position"), "position.quantity")
        assert quantity is not None
        currency = _currency(item.get("currency"), "position")
        timestamp = _epoch(item.get("timestamp"), "position.timestamp")
        if timestamp > fetched_at or fetched_at - timestamp > max_age:
            raise IBKRPortfolioError("A position is future-dated or stale.")
        symbol = item.get("contractDesc", item.get("description", str(conid_value)))
        symbol = _text(symbol, "position symbol", max_length=128)
        asset_class = _text(item.get("assetClass", item.get("secType", "UNKNOWN")),
                            "position asset class", max_length=32)
        result.append(IBKRPositionObservation(
            account_id=account_id,
            conid=conid_value,
            symbol=symbol,
            asset_class=asset_class,
            quantity=quantity,
            currency=currency,
            average_price=_decimal(item.get("avgPrice"), "position.avgPrice", optional=True),
            market_price=_decimal(item.get("marketPrice", item.get("mktPrice")),
                                  "position.marketPrice", optional=True),
            market_value=_decimal(item.get("marketValue", item.get("mktValue")),
                                  "position.marketValue", optional=True),
            provider_timestamp=timestamp,
            source="Client Portal Gateway /portfolio2/{accountId}/positions",
            payload_sha256=digest,
        ))
    return tuple(result)


__all__ = [
    "IBKRAccountMetric", "IBKRLocalGatewayTransport", "IBKRPortfolioAccount",
    "IBKRPortfolioError", "IBKRPositionObservation", "IBKRReadOnlyPortfolioClient",
    "IBKRReadOnlyPortfolioSnapshot",
]
