"""Single-request, read-only Crypto.com Exchange balance observation.

Credentials are held only for the duration of one explicit local import. This
module does not discover accounts, persist data, or expose trading endpoints.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Protocol


_BASE_URL = "https://api.crypto.com/exchange/v1"
_METHOD = "private/user-balance"
_POSITIONS_METHOD = "private/get-positions"
_OPEN_ORDERS_METHOD = "private/get-open-orders"
_TRANSACTIONS_METHOD = "private/get-transactions"
_TRADES_METHOD = "private/get-trades"
_FEE_RATE_METHOD = "private/get-fee-rate"
_BALANCE_HISTORY_METHOD = "private/user-balance-history"
_DEPOSIT_HISTORY_METHOD = "private/get-deposit-history"
_WITHDRAWAL_HISTORY_METHOD = "private/get-withdrawal-history"
_READ_METHODS = frozenset({_METHOD, _POSITIONS_METHOD, _OPEN_ORDERS_METHOD, _TRANSACTIONS_METHOD, _TRADES_METHOD,
                           _FEE_RATE_METHOD, _BALANCE_HISTORY_METHOD, _DEPOSIT_HISTORY_METHOD,
                           _WITHDRAWAL_HISTORY_METHOD})
_MAX_RESPONSE_BYTES = 1024 * 1024
_ASSET = re.compile(r"[A-Z0-9]{2,16}\Z")
_POSITION_INSTRUMENT = re.compile(r"[A-Z0-9._-]{2,64}\Z")
_TRADE_REQUEST_LOCK = threading.Lock()
_LAST_TRADE_REQUEST_MONOTONIC = 0.0
_TRANSACTION_REQUEST_LOCK = threading.Lock()
_LAST_TRANSACTION_REQUEST_MONOTONIC = 0.0
_FUNDING_REQUEST_LOCK = threading.Lock()
_LAST_FUNDING_REQUEST_MONOTONIC = 0.0


class CryptoComPortfolioError(ValueError):
    """Invalid or unavailable Crypto.com read-only balance observation."""


class JsonTransport(Protocol):
    def post_balance(self, body: bytes) -> tuple[object, bytes]: ...
    def post_positions(self, body: bytes) -> tuple[object, bytes]: ...
    def post_open_orders(self, body: bytes) -> tuple[object, bytes]: ...
    def post_transactions(self, body: bytes) -> tuple[object, bytes]: ...
    def post_trades(self, body: bytes) -> tuple[object, bytes]: ...
    def post_fee_rate(self, body: bytes) -> tuple[object, bytes]: ...
    def post_balance_history(self, body: bytes) -> tuple[object, bytes]: ...
    def post_deposit_history(self, body: bytes) -> tuple[object, bytes]: ...
    def post_withdrawal_history(self, body: bytes) -> tuple[object, bytes]: ...


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _safe_http_error(exc: urllib.error.HTTPError) -> CryptoComPortfolioError:
    """Extract only a numeric API code; never surface/store provider response text."""
    status = exc.code
    api_code = None
    try:
        raw = exc.read(64 * 1024 + 1)
        if len(raw) <= 64 * 1024:
            parsed = json.loads(raw)
            value = parsed.get("code") if isinstance(parsed, Mapping) else None
            if type(value) is int:
                api_code = value
    except Exception:
        pass
    finally:
        exc.close()
    detail = f", code API {api_code}" if api_code is not None else ""
    return CryptoComPortfolioError(f"Crypto.com a répondu HTTP {status}{detail}.")


class CryptoComExchangeTransport:
    """Verified-TLS transport pinned to explicit private read-only routes."""

    def post_balance(self, body: bytes) -> tuple[object, bytes]:
        return self._post(_METHOD, body)

    def post_positions(self, body: bytes) -> tuple[object, bytes]:
        return self._post(_POSITIONS_METHOD, body)

    def post_open_orders(self, body: bytes) -> tuple[object, bytes]:
        return self._post(_OPEN_ORDERS_METHOD, body)

    def post_transactions(self, body: bytes) -> tuple[object, bytes]:
        return self._post(_TRANSACTIONS_METHOD, body)

    def post_trades(self, body: bytes) -> tuple[object, bytes]:
        return self._post(_TRADES_METHOD, body)

    def post_fee_rate(self, body: bytes) -> tuple[object, bytes]:
        return self._post(_FEE_RATE_METHOD, body)

    def post_balance_history(self, body: bytes) -> tuple[object, bytes]:
        return self._post(_BALANCE_HISTORY_METHOD, body)

    def post_deposit_history(self, body: bytes) -> tuple[object, bytes]:
        return self._post(_DEPOSIT_HISTORY_METHOD, body)

    def post_withdrawal_history(self, body: bytes) -> tuple[object, bytes]:
        return self._post(_WITHDRAWAL_HISTORY_METHOD, body)

    def _post(self, method: str, body: bytes) -> tuple[object, bytes]:
        if method not in _READ_METHODS:
            raise CryptoComPortfolioError("Crypto.com route is not enabled by the read-only adapter.")
        request = urllib.request.Request(
            f"{_BASE_URL}/{method}", data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            context = ssl.create_default_context()
            if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                raise CryptoComPortfolioError("Crypto.com TLS verification must remain enabled.")
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
                _RejectRedirectHandler(),
            )
            response = opener.open(request, timeout=8)
            with response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except CryptoComPortfolioError:
            raise
        except urllib.error.HTTPError as exc:
            raise _safe_http_error(exc) from None
        except Exception:
            raise CryptoComPortfolioError("Crypto.com read-only request failed or TLS verification failed.") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise CryptoComPortfolioError("Crypto.com response exceeded the configured size limit.")
        try:
            payload = json.loads(raw, parse_float=Decimal, parse_int=int, parse_constant=_reject_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise CryptoComPortfolioError("Crypto.com returned invalid JSON or a non-finite number.") from None
        return payload, raw


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON number: {value}")


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, (bool, float)) or value is None:
        raise CryptoComPortfolioError(f"Invalid {field} in Crypto.com response.")
    try:
        number = value if isinstance(value, Decimal) else Decimal(value)  # type: ignore[arg-type]
    except (InvalidOperation, TypeError, ValueError):
        raise CryptoComPortfolioError(f"Invalid {field} in Crypto.com response.") from None
    if not number.is_finite():
        raise CryptoComPortfolioError(f"Non-finite {field} in Crypto.com response.")
    return number


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise CryptoComPortfolioError(f"Invalid {field} in Crypto.com response.")
    return value


def _has_isolated_positions(value: object) -> bool:
    if not isinstance(value, list):
        raise CryptoComPortfolioError("Invalid isolated positions in Crypto.com response.")
    return bool(value)


@dataclass(frozen=True, slots=True)
class CryptoComBalance:
    currency: str
    available: Decimal
    total_margin: Decimal
    initial_margin: Decimal
    total_cash: Decimal
    total_borrow: Decimal
    isolated_cash: Decimal
    is_liquidating: bool
    has_isolated_positions: bool
    source: str
    payload_sha256: str
    # Crypto.com total_initial_margin combines position requirements and
    # collateral haircut. Keep the components separately when the API returns
    # them so a haircut is not mistaken for an open leveraged position.
    total_position_im: Decimal | None = None
    total_haircut: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CryptoComPosition:
    instrument_name: str
    position_type: str
    quantity: Decimal
    cost_usd: Decimal
    open_position_pnl_usd: Decimal
    session_pnl_usd: Decimal
    updated_at: datetime
    source: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComWalletBalance:
    currency: str
    account_currency: str
    quantity: Decimal
    market_value: Decimal
    collateral_amount: Decimal
    max_withdrawal_balance: Decimal
    reserved_quantity: Decimal
    source: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComPortfolioSnapshot:
    fetched_at: datetime
    balances: tuple[CryptoComBalance, ...]
    payload_sha256: str
    positions: tuple[CryptoComPosition, ...] = ()
    positions_payload_sha256: str = ""
    wallet_balances: tuple[CryptoComWalletBalance, ...] = ()
    open_orders: tuple["CryptoComOpenOrder", ...] = ()
    open_orders_payload_sha256: str = ""
    provider: str = "Crypto.com Exchange"
    source: str = "POST /private/user-balance"
    authority_status: str = "BROKER_READ_ONLY_OBSERVATION_ONLY"
    persistent: bool = False
    usable_for_risk: bool = False
    usable_for_execution: bool = False


@dataclass(frozen=True, slots=True)
class CryptoComOpenOrder:
    account_id: str
    order_id: str
    client_oid: str | None
    instrument_name: str
    side: str
    order_type: str
    status: str
    quantity: Decimal
    cumulative_quantity: Decimal
    limit_price: Decimal | None
    created_at: datetime | None
    updated_at: datetime | None
    source: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComTransaction:
    journal_type: str
    journal_id: str
    transaction_qty: Decimal
    transaction_cost: Decimal
    realized_pnl: Decimal
    order_id: str | None
    trade_id: str | None
    instrument_name: str
    side: str | None
    occurred_at: datetime
    source: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComTransactionPage:
    start_time_ms: int
    end_time_ms: int
    items: tuple[CryptoComTransaction, ...]
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComTransactionPageSet:
    pages: tuple[CryptoComTransactionPage, ...]
    complete: bool
    incomplete_intervals: tuple[tuple[int, int], ...]
    request_count: int


@dataclass(frozen=True, slots=True)
class CryptoComTradePage:
    start_time_ns: int
    end_time_ns: int
    items: tuple["CryptoComExecutedTrade", ...]
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComTradePageSet:
    pages: tuple[CryptoComTradePage, ...]
    complete: bool
    incomplete_intervals: tuple[tuple[int, int], ...]
    request_count: int


@dataclass(frozen=True, slots=True)
class CryptoComExecutedTrade:
    trade_id: str
    order_id: str
    trade_match_id: str
    client_oid: str | None
    instrument_name: str
    side: str
    taker_side: str | None
    quantity: Decimal
    price: Decimal
    fees: Decimal
    fee_currency: str
    occurred_at: datetime
    timestamp_ns: str
    source: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComFeeSchedule:
    spot_tier: int
    maker_rate_bps: Decimal
    taker_rate_bps: Decimal
    observed_at: datetime
    source: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComBalanceHistoryPoint:
    currency: str
    timestamp: datetime
    total_cash: Decimal


@dataclass(frozen=True, slots=True)
class CryptoComBalanceHistory:
    timeframe: str
    observed_at: datetime
    points: tuple[CryptoComBalanceHistoryPoint, ...]
    source: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComFundingTransfer:
    direction: str
    transfer_id: str
    currency: str
    amount: Decimal
    fee: Decimal
    status: str
    created_at: datetime
    updated_at: datetime
    source: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComFundingHistoryPage:
    direction: str
    start_time_ms: int
    end_time_ms: int
    page: int
    page_size: int
    items: tuple[CryptoComFundingTransfer, ...]
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComFundingHistory:
    direction: str
    start_time_ms: int
    end_time_ms: int
    pages: tuple[CryptoComFundingHistoryPage, ...]
    complete: bool
    next_page: int | None
    request_count: int


class CryptoComReadOnlyPortfolioClient:
    """Read one authenticated balance snapshot; secrets are not retained."""

    def __init__(self, transport: JsonTransport | None = None, *,
                 clock: Callable[[], datetime] | None = None,
                 nonce: Callable[[], int] | None = None) -> None:
        self._transport = transport or CryptoComExchangeTransport()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._nonce = nonce or (lambda: time.time_ns() // 1_000_000)
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    def _next_nonce(self) -> int:
        with self._nonce_lock:
            value = self._nonce()
            if type(value) is not int or value <= 0:
                raise CryptoComPortfolioError("Unable to create a valid request timestamp.")
            value = max(value, self._last_nonce + 1)
            self._last_nonce = value
            return value

    def read_balance(self, api_key: str, api_secret: str) -> CryptoComPortfolioSnapshot:
        if not isinstance(api_key, str) or not api_key or len(api_key) > 256 or "\n" in api_key:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API key.")
        if not isinstance(api_secret, str) or not api_secret or len(api_secret) > 512 or "\n" in api_secret:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API secret.")
        nonce = self._next_nonce()
        # Crypto.com's official REST examples encode numeric request fields as strings.
        request_id = str(nonce)
        nonce_value = str(nonce)
        signature = hmac.new(
            api_secret.encode("utf-8"),
            f"{_METHOD}{request_id}{api_key}{nonce_value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        body = json.dumps({
            "id": request_id, "method": _METHOD, "api_key": api_key,
            "params": {}, "nonce": nonce_value, "sig": signature,
        }, separators=(",", ":")).encode("utf-8")
        try:
            payload, raw = self._transport.post_balance(body)
        except urllib.error.HTTPError as exc:
            raise _safe_http_error(exc) from None
        digest = hashlib.sha256(raw).hexdigest()
        if not isinstance(payload, Mapping):
            raise CryptoComPortfolioError("Crypto.com returned an invalid response envelope.")
        provider_code = payload.get("code")
        if type(provider_code) is not int:
            raise CryptoComPortfolioError("Crypto.com returned an invalid response code.")
        if provider_code != 0:
            if provider_code == 40101:
                raise CryptoComPortfolioError(
                    "Crypto.com a refuse l authentification (40101). Verifie que la cle complete et le secret "
                    "proviennent de la meme cle Crypto.com Exchange. Le secret est celui affiche une seule fois "
                    "lors de la creation; une cle Crypto.com App ne convient pas."
                )
            if provider_code == 40103:
                raise CryptoComPortfolioError(
                    "Crypto.com refuse l adresse IP de cette connexion (40103). Verifie la liste blanche IP "
                    "de la cle Exchange."
                )
            if provider_code == 40104:
                raise CryptoComPortfolioError(
                    "Crypto.com refuse cette requete selon le niveau du compte (40104)."
                )
            raise CryptoComPortfolioError(
                f"Crypto.com a refuse la lecture (code API {provider_code}). Verifie la cle Exchange et le droit Can Read."
             )
        if payload.get("method") != _METHOD:
            raise CryptoComPortfolioError("Crypto.com balance response did not match the requested method.")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise CryptoComPortfolioError("Crypto.com returned an invalid balance result.")
        rows = result.get("data")
        if not isinstance(rows, list):
            raise CryptoComPortfolioError("Crypto.com balance response has no valid data list.")
        balances: list[CryptoComBalance] = []
        wallet_balances: list[CryptoComWalletBalance] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise CryptoComPortfolioError("Crypto.com returned an invalid balance row.")
            currency = row.get("instrument_name")
            if not isinstance(currency, str) or not _ASSET.fullmatch(currency) or currency in seen:
                raise CryptoComPortfolioError("Crypto.com balance currencies must be unique and valid.")
            seen.add(currency)
            balances.append(CryptoComBalance(
                currency=currency,
                available=_decimal(row.get("total_available_balance"), "available balance"),
                total_margin=_decimal(row.get("total_margin_balance"), "margin balance"),
                initial_margin=_decimal(row.get("total_initial_margin"), "initial margin"),
                total_cash=_decimal(row.get("total_cash_balance"), "total cash balance"),
                total_borrow=_decimal(row.get("total_borrow", "0"), "total borrow"),
                isolated_cash=_decimal(row.get("total_isolated_cash_balance"), "isolated cash balance"),
                is_liquidating=_boolean(row.get("is_liquidating"), "liquidation status"),
                has_isolated_positions=_has_isolated_positions(row.get("isolated_positions", [])),
                source=_METHOD,
                payload_sha256=digest,
                total_position_im=(None if "total_position_im" not in row else
                                   _decimal(row["total_position_im"], "position initial margin")),
                total_haircut=(None if "total_haircut" not in row else
                               _decimal(row["total_haircut"], "collateral haircut")),
            ))
            position_rows = row.get("position_balances", [])
            if not isinstance(position_rows, list):
                raise CryptoComPortfolioError("Crypto.com returned invalid wallet asset balances.")
            wallet_seen: set[str] = set()
            for wallet_row in position_rows:
                if not isinstance(wallet_row, Mapping):
                    raise CryptoComPortfolioError("Crypto.com returned an invalid wallet asset row.")
                asset = wallet_row.get("instrument_name")
                if not isinstance(asset, str) or not _ASSET.fullmatch(asset) or asset in wallet_seen:
                    raise CryptoComPortfolioError("Crypto.com wallet asset currencies must be unique and valid.")
                wallet_seen.add(asset)
                wallet_balances.append(CryptoComWalletBalance(
                    currency=asset,
                    account_currency=currency,
                    quantity=_decimal(wallet_row.get("quantity"), "wallet quantity"),
                    market_value=_decimal(wallet_row.get("market_value"), "wallet market value"),
                    collateral_amount=_decimal(wallet_row.get("collateral_amount"), "wallet collateral amount"),
                    max_withdrawal_balance=_decimal(wallet_row.get("max_withdrawal_balance"), "wallet withdrawal balance"),
                    reserved_quantity=_decimal(wallet_row.get("reserved_qty", "0"), "wallet reserved quantity"),
                    source=_METHOD,
                    payload_sha256=digest,
                ))
        if not balances:
            raise CryptoComPortfolioError("Crypto.com returned no readable balance rows.")
        fetched = self._clock()
        if not isinstance(fetched, datetime) or fetched.tzinfo is None:
            raise CryptoComPortfolioError("Local collection timestamp must be timezone-aware.")
        return CryptoComPortfolioSnapshot(
            fetched_at=fetched.astimezone(timezone.utc), balances=tuple(balances),
            payload_sha256=digest, wallet_balances=tuple(wallet_balances),
        )


    def read_portfolio(self, api_key: str, api_secret: str) -> CryptoComPortfolioSnapshot:
        """Read wallet balances, open positions, and all open orders after one user action."""
        balance_snapshot = self.read_balance(api_key, api_secret)
        nonce = self._next_nonce()
        request_id = str(nonce)
        nonce_value = str(nonce)
        signature = hmac.new(
            api_secret.encode("utf-8"),
            f"{_POSITIONS_METHOD}{request_id}{api_key}{nonce_value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        body = json.dumps({
            "id": request_id, "method": _POSITIONS_METHOD, "api_key": api_key,
            "params": {}, "nonce": nonce_value, "sig": signature,
        }, separators=(",", ":")).encode("utf-8")
        try:
            payload, raw = self._transport.post_positions(body)
        except urllib.error.HTTPError as exc:
            raise _safe_http_error(exc) from None
        digest = hashlib.sha256(raw).hexdigest()
        if not isinstance(payload, Mapping):
            raise CryptoComPortfolioError("Crypto.com returned an invalid positions response envelope.")
        provider_code = payload.get("code")
        if type(provider_code) is not int:
            raise CryptoComPortfolioError("Crypto.com returned an invalid positions response code.")
        if provider_code != 0:
            raise CryptoComPortfolioError(
                f"Crypto.com refused the read-only positions request (API code {provider_code})."
            )
        if payload.get("method") != _POSITIONS_METHOD:
            raise CryptoComPortfolioError("Crypto.com positions response did not match the requested method.")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise CryptoComPortfolioError("Crypto.com returned an invalid positions result.")
        rows = result.get("data")
        if not isinstance(rows, list):
            raise CryptoComPortfolioError("Crypto.com positions response has no valid data list.")
        positions = self._parse_positions(rows, digest)
        open_orders, open_orders_digest = self.read_open_orders(api_key, api_secret)
        fetched = self._clock()
        if not isinstance(fetched, datetime) or fetched.tzinfo is None:
            raise CryptoComPortfolioError("Local collection timestamp must be timezone-aware.")
        return replace(
            balance_snapshot,
            fetched_at=fetched.astimezone(timezone.utc),
            positions=positions,
            positions_payload_sha256=digest,
            source="POST /private/user-balance; POST /private/get-positions; POST /private/get-open-orders",
            open_orders=open_orders,
            open_orders_payload_sha256=open_orders_digest,
        )

    def read_open_orders(self, api_key: str, api_secret: str) -> tuple[tuple[CryptoComOpenOrder, ...], str]:
        if not isinstance(api_key, str) or not api_key or len(api_key) > 256 or "\n" in api_key:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API key.")
        if not isinstance(api_secret, str) or not api_secret or len(api_secret) > 512 or "\n" in api_secret:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API secret.")
        nonce = self._next_nonce()
        request_id = str(nonce)
        nonce_value = str(nonce)
        signature = hmac.new(api_secret.encode("utf-8"),
            f"{_OPEN_ORDERS_METHOD}{request_id}{api_key}{nonce_value}".encode("utf-8"), hashlib.sha256).hexdigest()
        body = json.dumps({"id": request_id, "method": _OPEN_ORDERS_METHOD, "api_key": api_key,
            "params": {}, "nonce": nonce_value, "sig": signature}, separators=(",", ":")).encode("utf-8")
        try:
            payload, raw = self._transport.post_open_orders(body)
        except urllib.error.HTTPError as exc:
            raise _safe_http_error(exc) from None
        digest = hashlib.sha256(raw).hexdigest()
        if not isinstance(payload, Mapping) or type(payload.get("code")) is not int:
            raise CryptoComPortfolioError("Crypto.com returned an invalid open-orders response envelope.")
        if payload["code"] != 0:
            raise CryptoComPortfolioError(f"Crypto.com refused the open-orders request (API code {payload['code']}).")
        if payload.get("method") != _OPEN_ORDERS_METHOD:
            raise CryptoComPortfolioError("Crypto.com open-orders response did not match the requested method.")
        result = payload.get("result")
        rows = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(rows, list):
            raise CryptoComPortfolioError("Crypto.com open-orders response has no valid data list.")
        return self._parse_open_orders(rows, digest), digest

    def read_transactions(self, api_key: str, api_secret: str, *, start_time_ms: int,
                          end_time_ms: int, limit: int = 100) -> tuple[tuple[CryptoComTransaction, ...], str]:
        """Read one bounded transaction-history page; this is evidence, never Risk authority."""
        if not isinstance(api_key, str) or not api_key or len(api_key) > 256 or "\n" in api_key:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API key.")
        if not isinstance(api_secret, str) or not api_secret or len(api_secret) > 512 or "\n" in api_secret:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API secret.")
        if (type(start_time_ms) is not int or type(end_time_ms) is not int or start_time_ms < 0
                or end_time_ms <= start_time_ms or end_time_ms > time.time_ns() // 1_000_000 + 60_000):
            raise CryptoComPortfolioError("Transaction time bounds must be valid Unix milliseconds.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CryptoComPortfolioError("Transaction page size must be between 1 and 100.")
        params = {"start_time": str(start_time_ms), "end_time": str(end_time_ms), "limit": limit}
        nonce = self._next_nonce()
        request_id, nonce_value = str(nonce), str(nonce)
        parameter_string = "".join(key + str(params[key]) for key in sorted(params))
        signature = hmac.new(api_secret.encode("utf-8"),
            f"{_TRANSACTIONS_METHOD}{request_id}{api_key}{parameter_string}{nonce_value}".encode("utf-8"),
            hashlib.sha256).hexdigest()
        body = json.dumps({"id": request_id, "method": _TRANSACTIONS_METHOD, "api_key": api_key,
            "params": params, "nonce": nonce_value, "sig": signature}, separators=(",", ":")).encode("utf-8")
        global _LAST_TRANSACTION_REQUEST_MONOTONIC
        try:
            # Private REST calls are rate-limited per method and API key.
            # Keep this endpoint below the documented 3 requests / 100 ms.
            with _TRANSACTION_REQUEST_LOCK:
                wait_for = (1 / 30) - (time.monotonic() - _LAST_TRANSACTION_REQUEST_MONOTONIC)
                if wait_for > 0:
                    time.sleep(wait_for)
                _LAST_TRANSACTION_REQUEST_MONOTONIC = time.monotonic()
                payload, raw = self._transport.post_transactions(body)
        except urllib.error.HTTPError as exc:
            raise _safe_http_error(exc) from None
        digest = hashlib.sha256(raw).hexdigest()
        if not isinstance(payload, Mapping) or type(payload.get("code")) is not int:
            raise CryptoComPortfolioError("Crypto.com returned an invalid transaction response envelope.")
        if payload["code"] != 0:
            raise CryptoComPortfolioError(f"Crypto.com refused the transaction-history request (API code {payload['code']}).")
        if payload.get("method") != _TRANSACTIONS_METHOD:
            raise CryptoComPortfolioError("Crypto.com transaction response did not match the requested method.")
        result = payload.get("result")
        rows = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(rows, list) or len(rows) > limit:
            raise CryptoComPortfolioError("Crypto.com transaction response has an invalid data page.")
        return self._parse_transactions(rows, digest, start_time_ms, end_time_ms), digest

    def read_transactions_partitioned(self, api_key: str, api_secret: str, *, start_time_ms: int,
                                      end_time_ms: int, limit: int = 100,
                                      max_requests: int = 16) -> CryptoComTransactionPageSet:
        """Read a bounded time range, splitting capped pages without claiming account-wide completeness."""
        if (type(start_time_ms) is not int or type(end_time_ms) is not int
                or start_time_ms < 0 or start_time_ms >= end_time_ms
                or end_time_ms - start_time_ms > 7 * 86_400_000
                or type(limit) is not int or not 1 <= limit <= 100
                or type(max_requests) is not int or not 1 <= max_requests <= 64):
            raise CryptoComPortfolioError("Transaction pagination bounds are invalid or exceed configured limits.")
        pending = [(start_time_ms, end_time_ms)]
        pages: list[CryptoComTransactionPage] = []
        incomplete: list[tuple[int, int]] = []
        observed_ids: set[str] = set()
        request_count = 0
        while pending and request_count < max_requests:
            start, end = pending.pop()
            items, digest = self.read_transactions(
                api_key, api_secret, start_time_ms=start, end_time_ms=end, limit=limit,
            )
            request_count += 1
            if len(items) == limit and end - start > 1:
                midpoint = start + (end - start) // 2
                # LIFO: visit the earlier half first; [start, midpoint) and
                # [midpoint, end) are disjoint under Crypto.com's exclusive end.
                pending.append((midpoint, end))
                pending.append((start, midpoint))
                continue
            if len(items) == limit:
                incomplete.append((start, end))
            for item in items:
                if item.journal_id in observed_ids:
                    raise CryptoComPortfolioError("Crypto.com returned a duplicate journal across transaction pages.")
                observed_ids.add(item.journal_id)
            pages.append(CryptoComTransactionPage(start, end, items, digest))
        incomplete.extend(pending)
        pages.sort(key=lambda page: (page.start_time_ms, page.end_time_ms))
        incomplete = sorted(set(incomplete))
        return CryptoComTransactionPageSet(
            tuple(pages), not incomplete, tuple(incomplete), request_count,
        )

    def read_trades(self, api_key: str, api_secret: str, *, start_time_ns: int,
                    end_time_ns: int, limit: int = 100) -> tuple[tuple[CryptoComExecutedTrade, ...], str]:
        """Recover bounded executed fills; a capped page is evidence, not completeness."""
        if not isinstance(api_key, str) or not api_key or len(api_key) > 256 or "\n" in api_key:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API key.")
        if not isinstance(api_secret, str) or not api_secret or len(api_secret) > 512 or "\n" in api_secret:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API secret.")
        now_ns = time.time_ns()
        if (type(start_time_ns) is not int or type(end_time_ns) is not int or start_time_ns < 0
                or end_time_ns <= start_time_ns or end_time_ns > now_ns + 60_000_000_000
                or end_time_ns - start_time_ns > 7 * 86_400_000_000_000):
            raise CryptoComPortfolioError("Trade time bounds must be valid Unix nanoseconds within seven days.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CryptoComPortfolioError("Trade page size must be between 1 and 100.")
        params = {"start_time": str(start_time_ns), "end_time": str(end_time_ns), "limit": limit}
        nonce = self._next_nonce()
        request_id, nonce_value = str(nonce), str(nonce)
        parameter_string = "".join(key + str(params[key]) for key in sorted(params))
        signature = hmac.new(api_secret.encode("utf-8"),
            f"{_TRADES_METHOD}{request_id}{api_key}{parameter_string}{nonce_value}".encode("utf-8"),
            hashlib.sha256).hexdigest()
        body = json.dumps({"id": request_id, "method": _TRADES_METHOD, "api_key": api_key,
            "params": params, "nonce": nonce_value, "sig": signature}, separators=(",", ":")).encode("utf-8")
        global _LAST_TRADE_REQUEST_MONOTONIC
        try:
            # Crypto.com documents a one-request-per-second REST limit for this route.
            with _TRADE_REQUEST_LOCK:
                wait_for = 1.0 - (time.monotonic() - _LAST_TRADE_REQUEST_MONOTONIC)
                if wait_for > 0:
                    time.sleep(wait_for)
                _LAST_TRADE_REQUEST_MONOTONIC = time.monotonic()
                payload, raw = self._transport.post_trades(body)
        except urllib.error.HTTPError as exc:
            raise _safe_http_error(exc) from None
        digest = hashlib.sha256(raw).hexdigest()
        if not isinstance(payload, Mapping) or type(payload.get("code")) is not int:
            raise CryptoComPortfolioError("Crypto.com returned an invalid trade response envelope.")
        if payload["code"] != 0:
            raise CryptoComPortfolioError(f"Crypto.com refused the trade-history request (API code {payload['code']}).")
        if payload.get("method") != _TRADES_METHOD:
            raise CryptoComPortfolioError("Crypto.com trade response did not match the requested method.")
        result = payload.get("result")
        rows = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(rows, list) or len(rows) > limit:
            raise CryptoComPortfolioError("Crypto.com trade response has an invalid data page.")
        return self._parse_trades(rows, digest, start_time_ns, end_time_ns), digest

    def read_trades_partitioned(self, api_key: str, api_secret: str, *, start_time_ns: int,
                                end_time_ns: int, limit: int = 100,
                                max_requests: int = 16) -> CryptoComTradePageSet:
        """Split capped trade queries into disjoint intervals; never infer full-history coverage."""
        if (type(start_time_ns) is not int or type(end_time_ns) is not int
                or start_time_ns < 0 or start_time_ns >= end_time_ns
                or end_time_ns - start_time_ns > 7 * 86_400_000_000_000
                or type(limit) is not int or not 1 <= limit <= 100
                or type(max_requests) is not int or not 1 <= max_requests <= 64):
            raise CryptoComPortfolioError("Trade pagination bounds are invalid or exceed configured limits.")
        pending = [(start_time_ns, end_time_ns)]
        pages: list[CryptoComTradePage] = []
        incomplete: list[tuple[int, int]] = []
        observed_ids: set[tuple[str, str]] = set()
        request_count = 0
        while pending and request_count < max_requests:
            start, end = pending.pop()
            items, digest = self.read_trades(
                api_key, api_secret, start_time_ns=start, end_time_ns=end, limit=limit,
            )
            request_count += 1
            if len(items) == limit and end - start > 1:
                midpoint = start + (end - start) // 2
                pending.append((midpoint, end))
                pending.append((start, midpoint))
                continue
            if len(items) == limit:
                incomplete.append((start, end))
            for item in items:
                identity = (item.instrument_name, item.trade_id)
                if identity in observed_ids:
                    raise CryptoComPortfolioError("Crypto.com returned a duplicate trade across history pages.")
                observed_ids.add(identity)
            pages.append(CryptoComTradePage(start, end, items, digest))
        incomplete.extend(pending)
        pages.sort(key=lambda page: (page.start_time_ns, page.end_time_ns))
        incomplete = sorted(set(incomplete))
        return CryptoComTradePageSet(tuple(pages), not incomplete, tuple(incomplete), request_count)

    def read_fee_schedule(self, api_key: str, api_secret: str) -> CryptoComFeeSchedule:
        """Read the authenticated account's effective Spot maker/taker fee rates."""
        if not isinstance(api_key, str) or not api_key or len(api_key) > 256 or "\n" in api_key:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API key.")
        if not isinstance(api_secret, str) or not api_secret or len(api_secret) > 512 or "\n" in api_secret:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API secret.")
        nonce = self._next_nonce()
        request_id, nonce_value = str(nonce), str(nonce)
        signature = hmac.new(api_secret.encode("utf-8"),
            f"{_FEE_RATE_METHOD}{request_id}{api_key}{nonce_value}".encode("utf-8"), hashlib.sha256).hexdigest()
        body = json.dumps({"id": request_id, "method": _FEE_RATE_METHOD, "api_key": api_key,
            "params": {}, "nonce": nonce_value, "sig": signature}, separators=(",", ":")).encode("utf-8")
        try:
            payload, raw = self._transport.post_fee_rate(body)
        except urllib.error.HTTPError as exc:
            raise _safe_http_error(exc) from None
        digest = hashlib.sha256(raw).hexdigest()
        if not isinstance(payload, Mapping) or type(payload.get("code")) is not int:
            raise CryptoComPortfolioError("Crypto.com returned an invalid fee-rate response envelope.")
        if payload["code"] != 0:
            raise CryptoComPortfolioError(f"Crypto.com refused the fee-rate request (API code {payload['code']}).")
        if payload.get("method") != _FEE_RATE_METHOD or not isinstance(payload.get("result"), Mapping):
            raise CryptoComPortfolioError("Crypto.com fee-rate response did not match the requested method.")
        result = payload["result"]
        tier = result.get("spot_tier")
        if isinstance(tier, str) and tier.isdecimal() and len(tier) <= 4:
            tier = int(tier)
        if type(tier) is not int or not 0 <= tier <= 1000:
            raise CryptoComPortfolioError("Crypto.com returned an invalid Spot fee tier.")
        maker = _decimal(result.get("effective_spot_maker_rate_bps"), "Spot maker fee rate")
        taker = _decimal(result.get("effective_spot_taker_rate_bps"), "Spot taker fee rate")
        if maker < 0 or taker < 0 or maker > 10_000 or taker > 10_000:
            raise CryptoComPortfolioError("Crypto.com returned an out-of-range Spot fee rate.")
        observed_at = self._clock()
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise CryptoComPortfolioError("Local fee-rate observation time must be timezone-aware.")
        return CryptoComFeeSchedule(tier, maker, taker, observed_at.astimezone(timezone.utc),
            _FEE_RATE_METHOD, digest)

    def read_balance_history(self, api_key: str, api_secret: str, *,
                             timeframe: str = "H1", limit: int = 24) -> CryptoComBalanceHistory:
        """Read bounded exchange cash history; it is not portfolio equity or Risk PnL."""
        if not isinstance(api_key, str) or not api_key or len(api_key) > 256 or "\n" in api_key:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API key.")
        if not isinstance(api_secret, str) or not api_secret or len(api_secret) > 512 or "\n" in api_secret:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API secret.")
        if timeframe not in {"H1", "D1"} or type(limit) is not int or not 1 <= limit <= (120 if timeframe == "H1" else 30):
            raise CryptoComPortfolioError("Balance-history timeframe or page size is invalid.")
        params = {"timeframe": timeframe, "limit": limit}
        parameter_string = "".join(key + str(params[key]) for key in sorted(params))
        nonce = self._next_nonce()
        request_id, nonce_value = str(nonce), str(nonce)
        signature = hmac.new(api_secret.encode("utf-8"),
            f"{_BALANCE_HISTORY_METHOD}{request_id}{api_key}{parameter_string}{nonce_value}".encode("utf-8"),
            hashlib.sha256).hexdigest()
        body = json.dumps({"id": request_id, "method": _BALANCE_HISTORY_METHOD, "api_key": api_key,
            "params": params, "nonce": nonce_value, "sig": signature}, separators=(",", ":")).encode("utf-8")
        try:
            payload, raw = self._transport.post_balance_history(body)
        except urllib.error.HTTPError as exc:
            raise _safe_http_error(exc) from None
        digest = hashlib.sha256(raw).hexdigest()
        if not isinstance(payload, Mapping) or type(payload.get("code")) is not int:
            raise CryptoComPortfolioError("Crypto.com returned an invalid balance-history response envelope.")
        if payload["code"] != 0:
            raise CryptoComPortfolioError(f"Crypto.com refused the balance-history request (API code {payload['code']}).")
        result = payload.get("result")
        if payload.get("method") != _BALANCE_HISTORY_METHOD or not isinstance(result, Mapping):
            raise CryptoComPortfolioError("Crypto.com balance-history response did not match the requested method.")
        currency = result.get("instrument_name")
        rows = result.get("data")
        if (not isinstance(currency, str) or not _ASSET.fullmatch(currency)
                or not isinstance(rows, list) or not rows or len(rows) > limit):
            raise CryptoComPortfolioError("Crypto.com balance-history response is incomplete or invalid.")
        points: list[CryptoComBalanceHistoryPoint] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise CryptoComPortfolioError("Crypto.com returned an invalid balance-history row.")
            timestamp = row.get("t")
            if isinstance(timestamp, str) and timestamp.isdecimal():
                timestamp = int(timestamp)
            if type(timestamp) is not int or timestamp <= 0 or timestamp in seen:
                raise CryptoComPortfolioError("Crypto.com balance-history timestamps are invalid or duplicated.")
            seen.add(timestamp)
            try:
                point_time = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
                total_cash = _decimal(row.get("c"), "historical total cash")
            except (ValueError, OverflowError, OSError):
                raise CryptoComPortfolioError("Crypto.com balance-history values are invalid.") from None
            points.append(CryptoComBalanceHistoryPoint(currency, point_time, total_cash))
        points.sort(key=lambda point: point.timestamp)
        observed_at = self._clock()
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise CryptoComPortfolioError("Local balance-history observation time must be timezone-aware.")
        return CryptoComBalanceHistory(timeframe, observed_at.astimezone(timezone.utc), tuple(points),
                                       _BALANCE_HISTORY_METHOD, digest)

    def read_funding_history(self, api_key: str, api_secret: str, *, direction: str,
                             start_time_ms: int, end_time_ms: int, page_size: int = 200,
                             max_pages: int = 16) -> CryptoComFundingHistory:
        """Read bounded deposit or withdrawal pages; this is evidence, never Risk authority."""
        if not isinstance(api_key, str) or not api_key or len(api_key) > 256 or "\n" in api_key:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API key.")
        if not isinstance(api_secret, str) or not api_secret or len(api_secret) > 512 or "\n" in api_secret:
            raise CryptoComPortfolioError("Enter a valid Crypto.com API secret.")
        if direction not in {"DEPOSIT", "WITHDRAWAL"}:
            raise CryptoComPortfolioError("Funding-history direction must be DEPOSIT or WITHDRAWAL.")
        now_ms = time.time_ns() // 1_000_000
        if (type(start_time_ms) is not int or type(end_time_ms) is not int or start_time_ms < 0
                or start_time_ms >= end_time_ms or end_time_ms > now_ms + 60_000
                or end_time_ms - start_time_ms > 90 * 86_400_000):
            raise CryptoComPortfolioError("Funding-history range must be valid and no longer than 90 days.")
        if type(page_size) is not int or not 1 <= page_size <= 200:
            raise CryptoComPortfolioError("Funding-history page size must be between 1 and 200.")
        if type(max_pages) is not int or not 1 <= max_pages <= 64:
            raise CryptoComPortfolioError("Funding-history page limit must be between 1 and 64.")

        method = _DEPOSIT_HISTORY_METHOD if direction == "DEPOSIT" else _WITHDRAWAL_HISTORY_METHOD
        transport_call = (self._transport.post_deposit_history if direction == "DEPOSIT"
                          else self._transport.post_withdrawal_history)
        rows_key = "deposit_list" if direction == "DEPOSIT" else "withdrawal_list"
        valid_statuses = {"0", "1", "2", "3"} if direction == "DEPOSIT" else {"0", "1", "2", "3", "4", "5", "6"}
        pages: list[CryptoComFundingHistoryPage] = []
        seen_ids: set[str] = set()
        complete = False
        next_page = 0
        global _LAST_FUNDING_REQUEST_MONOTONIC
        for current_page in range(max_pages):
            params = {"start_ts": start_time_ms, "end_ts": end_time_ms,
                      "page_size": page_size, "page": current_page}
            parameter_string = "".join(key + str(params[key]) for key in sorted(params))
            nonce = self._next_nonce()
            request_id, nonce_value = str(nonce), str(nonce)
            signature = hmac.new(api_secret.encode("utf-8"),
                f"{method}{request_id}{api_key}{parameter_string}{nonce_value}".encode("utf-8"),
                hashlib.sha256).hexdigest()
            body = json.dumps({"id": request_id, "method": method, "api_key": api_key,
                "params": params, "nonce": nonce_value, "sig": signature},
                separators=(",", ":")).encode("utf-8")
            try:
                # Bound the shared request cadence below the Exchange private REST limit.
                with _FUNDING_REQUEST_LOCK:
                    wait_for = (1 / 30) - (time.monotonic() - _LAST_FUNDING_REQUEST_MONOTONIC)
                    if wait_for > 0:
                        time.sleep(wait_for)
                    _LAST_FUNDING_REQUEST_MONOTONIC = time.monotonic()
                    payload, raw = transport_call(body)
            except urllib.error.HTTPError as exc:
                raise _safe_http_error(exc) from None
            digest = hashlib.sha256(raw).hexdigest()
            if not isinstance(payload, Mapping) or type(payload.get("code")) is not int:
                raise CryptoComPortfolioError("Crypto.com returned an invalid funding-history response envelope.")
            if payload["code"] != 0:
                raise CryptoComPortfolioError(
                    f"Crypto.com refused the {direction.casefold()} history request (API code {payload['code']}).")
            result = payload.get("result")
            rows = result.get(rows_key) if payload.get("method") == method and isinstance(result, Mapping) else None
            if not isinstance(rows, list) or len(rows) > page_size:
                raise CryptoComPortfolioError("Crypto.com funding-history response has an invalid data page.")
            items: list[CryptoComFundingTransfer] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    raise CryptoComPortfolioError("Crypto.com returned an invalid funding-history row.")
                transfer_id, currency = row.get("id"), row.get("currency")
                if type(transfer_id) is int:
                    transfer_id = str(transfer_id)
                status = row.get("status")
                if type(status) is int:
                    status = str(status)
                if (not isinstance(transfer_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", transfer_id) is None
                        or transfer_id in seen_ids or not isinstance(currency, str) or _ASSET.fullmatch(currency) is None
                        or not isinstance(status, str) or status not in valid_statuses):
                    raise CryptoComPortfolioError("Crypto.com funding-history identity or status is invalid.")
                created_raw, updated_raw = row.get("create_time"), row.get("update_time")
                if isinstance(created_raw, str) and created_raw.isdecimal():
                    created_raw = int(created_raw)
                if isinstance(updated_raw, str) and updated_raw.isdecimal():
                    updated_raw = int(updated_raw)
                if (type(created_raw) is not int or type(updated_raw) is not int
                        or not start_time_ms <= created_raw < end_time_ms or updated_raw < created_raw):
                    raise CryptoComPortfolioError("Crypto.com funding-history timestamps are invalid.")
                amount = _decimal(row.get("amount"), "funding amount")
                fee = _decimal(row.get("fee"), "funding fee")
                if amount <= 0 or fee < 0:
                    raise CryptoComPortfolioError("Crypto.com funding-history amount or fee is invalid.")
                try:
                    created_at = datetime.fromtimestamp(created_raw / 1000, timezone.utc)
                    updated_at = datetime.fromtimestamp(updated_raw / 1000, timezone.utc)
                except (ValueError, OverflowError, OSError):
                    raise CryptoComPortfolioError("Crypto.com funding-history timestamp is out of range.") from None
                items.append(CryptoComFundingTransfer(direction, transfer_id, currency, amount, fee, status,
                    created_at, updated_at, method, digest))
                seen_ids.add(transfer_id)
            pages.append(CryptoComFundingHistoryPage(direction, start_time_ms, end_time_ms,
                current_page, page_size, tuple(items), digest))
            if len(rows) < page_size:
                complete = True
                next_page = current_page + 1
                break
            next_page = current_page + 1
        return CryptoComFundingHistory(direction, start_time_ms, end_time_ms, tuple(pages),
                                       complete, None if complete else next_page, len(pages))

    @staticmethod
    def _parse_transactions(rows: list[object], digest: str, start_time_ms: int,
                            end_time_ms: int) -> tuple[CryptoComTransaction, ...]:
        # Crypto.com Exchange has returned OFFCHAIN_DEPOSIT on the live read-only
        # endpoint, while its published schema documents DEPOSIT/WITHDRAW and
        # older account records can use ONCHAIN_* names. Keep all explicit
        # variants so an unfamiliar-but-valid funding journal cannot hide the
        # rest of the account history behind a page-level parse failure.
        journal_types = {"TRADING", "TRADE_FEE", "ONCHAIN_WITHDRAWAL", "ONCHAIN_DEPOSIT",
            "OFFCHAIN_WITHDRAWAL", "OFFCHAIN_DEPOSIT", "WITHDRAW", "DEPOSIT",
            "WITHDRAW_FEE", "ROLLBACK_DEPOSIT", "ROLLBACK_WITHDRAW", "FUNDING",
            "REALIZED_PNL", "INSURANCE_FUND", "SOCIALIZED_LOSS", "LIQUIDATION_FEE", "SESSION_RESET",
            "ADJUSTMENT", "SESSION_SETTLE", "UNCOVERED_LOSS", "ADMIN_ADJUSTMENT", "DELIST",
            "SETTLEMENT_FEE", "AUTO_CONVERSION", "MANUAL_CONVERSION", "SUBACCOUNT_TX",
            "FIAT_WITHDRAWAL_CANCEL", "MARGIN_TRADE_INTEREST"}
        transactions: list[CryptoComTransaction] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise CryptoComPortfolioError("Crypto.com returned an invalid transaction row.")
            kind, raw_journal_id = row.get("journal_type"), row.get("journal_id")
            journal_id = str(raw_journal_id) if type(raw_journal_id) is int else raw_journal_id
            instrument = row.get("instrument_name")
            side = row.get("side")
            # The live Exchange endpoint uses its string sentinel for an
            # absent side on account funding events; it is not a trade side.
            side = None if side in ("", "NULL_VAL") else side
            if (kind not in journal_types or not isinstance(journal_id, str) or not journal_id.isdecimal()
                    or len(journal_id) > 32 or journal_id in seen
                    or not isinstance(instrument, str) or not _POSITION_INSTRUMENT.fullmatch(instrument)
                    or (side is not None and side not in {"BUY", "SELL"})):
                raise CryptoComPortfolioError("Crypto.com transaction identity or classification is invalid.")
            raw_timestamp = row.get("event_timestamp_ms")
            if isinstance(raw_timestamp, str) and raw_timestamp.isdecimal():
                raw_timestamp = int(raw_timestamp)
            if type(raw_timestamp) is not int or not start_time_ms <= raw_timestamp < end_time_ms:
                raise CryptoComPortfolioError("Crypto.com transaction timestamp is outside the requested interval.")
            order_id = row.get("order_id")
            trade_id = row.get("trade_id")
            order_id = str(order_id) if type(order_id) is int else order_id
            trade_id = str(trade_id) if type(trade_id) is int else trade_id
            # Crypto.com returns empty strings for absent references on
            # non-trading journals (for example OFFCHAIN_DEPOSIT). Normalize
            # those documented-as-optional identifiers to None.
            order_id = None if order_id == "" else order_id
            trade_id = None if trade_id == "" else trade_id
            if ((order_id is not None and (not isinstance(order_id, str) or not order_id.isdecimal()))
                    or (trade_id is not None and (not isinstance(trade_id, str) or not trade_id.isdecimal()))):
                raise CryptoComPortfolioError("Crypto.com transaction references are invalid.")
            seen.add(journal_id)
            transactions.append(CryptoComTransaction(kind, journal_id,
                _decimal(row.get("transaction_qty"), "transaction quantity"),
                _decimal(row.get("transaction_cost"), "transaction cost"),
                _decimal(row.get("realized_pnl"), "realized PnL"), order_id, trade_id, instrument, side,
                datetime.fromtimestamp(raw_timestamp / 1000, timezone.utc), _TRANSACTIONS_METHOD, digest))
        return tuple(sorted(transactions, key=lambda item: (item.occurred_at, item.journal_id)))

    @staticmethod
    def _parse_trades(rows: list[object], digest: str, start_time_ns: int,
                      end_time_ns: int) -> tuple[CryptoComExecutedTrade, ...]:
        trades: list[CryptoComExecutedTrade] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise CryptoComPortfolioError("Crypto.com returned an invalid trade row.")
            instrument = row.get("instrument_name")
            side, taker_side = row.get("side"), row.get("taker_side")
            fee_currency = row.get("fee_instrument_name")
            raw_ns = row.get("create_time_ns")
            if isinstance(raw_ns, int) and not isinstance(raw_ns, bool):
                raw_ns = str(raw_ns)
            raw_ms = row.get("create_time")
            if isinstance(raw_ms, str) and raw_ms.isdecimal():
                raw_ms = int(raw_ms)
            identifiers = {}
            for field in ("trade_id", "order_id", "trade_match_id"):
                value = row.get(field)
                if type(value) is int:
                    value = str(value)
                if not isinstance(value, str) or not value.isdecimal() or len(value) > 32:
                    raise CryptoComPortfolioError("Crypto.com trade identifiers are invalid.")
                identifiers[field] = value
            client_oid = row.get("client_oid")
            if client_oid is not None and (not isinstance(client_oid, str)
                    or re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", client_oid) is None):
                raise CryptoComPortfolioError("Crypto.com trade client order reference is invalid.")
            if (not isinstance(instrument, str) or not _POSITION_INSTRUMENT.fullmatch(instrument)
                    or side not in {"BUY", "SELL"} or taker_side not in {None, "", "MAKER", "TAKER"}
                    or not isinstance(fee_currency, str) or not _ASSET.fullmatch(fee_currency)
                    or not isinstance(raw_ns, str) or not raw_ns.isdecimal() or len(raw_ns) > 24
                    or type(raw_ms) is not int or raw_ms <= 0):
                raise CryptoComPortfolioError("Crypto.com trade identity or classification is invalid.")
            timestamp_ns = int(raw_ns)
            if (not start_time_ns <= timestamp_ns < end_time_ns
                    or timestamp_ns // 1_000_000 != raw_ms):
                raise CryptoComPortfolioError("Crypto.com trade timestamp is outside the requested interval.")
            identity = (instrument, identifiers["trade_id"])
            if identity in seen:
                raise CryptoComPortfolioError("Crypto.com returned duplicate trade identities.")
            seen.add(identity)
            quantity = _decimal(row.get("traded_quantity"), "traded quantity")
            price = _decimal(row.get("traded_price"), "traded price")
            fees = _decimal(row.get("fees"), "trade fee")
            if quantity <= 0 or price <= 0:
                raise CryptoComPortfolioError("Crypto.com trade quantity and price must be positive.")
            try:
                occurred_at = datetime.fromtimestamp(raw_ms / 1000, timezone.utc)
            except (ValueError, OverflowError, OSError):
                raise CryptoComPortfolioError("Crypto.com trade timestamp is invalid.") from None
            trades.append(CryptoComExecutedTrade(identifiers["trade_id"], identifiers["order_id"],
                identifiers["trade_match_id"], client_oid, instrument, side,
                taker_side or None, quantity, price, fees, fee_currency, occurred_at, raw_ns,
                _TRADES_METHOD, digest))
        return tuple(sorted(trades, key=lambda item: (item.timestamp_ns, item.instrument_name, item.trade_id)))

    @staticmethod
    def _parse_open_orders(rows: list[object], digest: str) -> tuple[CryptoComOpenOrder, ...]:
        orders: list[CryptoComOpenOrder] = []
        seen: set[tuple[str, str]] = set()
        order_types = {"LIMIT", "MARKET", "STOP_LOSS", "STOP_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"}
        statuses = {"PENDING", "NEW", "ACTIVE", "FILLED", "CANCELED", "REJECTED", "EXPIRED"}

        def timestamp(value: object, field: str) -> datetime | None:
            if value is None:
                return None
            if isinstance(value, str) and value.isdecimal():
                value = int(value)
            try:
                if type(value) is not int or value <= 0:
                    raise ValueError
                return datetime.fromtimestamp(value / 1000, timezone.utc)
            except (OverflowError, OSError, ValueError):
                raise CryptoComPortfolioError(f"Crypto.com open-order {field} is invalid.") from None

        for row in rows:
            if not isinstance(row, Mapping):
                raise CryptoComPortfolioError("Crypto.com returned an invalid open-order row.")
            account_id = row.get("account_id")
            raw_id = row.get("order_id")
            order_id = str(raw_id) if type(raw_id) is int and raw_id > 0 else raw_id
            client_oid = row.get("client_oid")
            instrument = row.get("instrument_name")
            side, order_type, status = row.get("side"), row.get("order_type"), row.get("status")
            if (not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 128
                    or not isinstance(order_id, str) or not order_id.isdecimal() or len(order_id) > 32
                    or (client_oid is not None and (not isinstance(client_oid, str) or len(client_oid) > 128))
                    or not isinstance(instrument, str) or not _POSITION_INSTRUMENT.fullmatch(instrument)
                    or not isinstance(side, str) or side not in {"BUY", "SELL"}
                    or not isinstance(order_type, str) or order_type not in order_types
                    or not isinstance(status, str) or status not in statuses):
                raise CryptoComPortfolioError("Crypto.com open-order identity or status is invalid.")
            identity = (account_id, order_id)
            if identity in seen:
                raise CryptoComPortfolioError("Crypto.com returned a duplicate open order.")
            seen.add(identity)
            quantity = _decimal(row.get("quantity"), "open-order quantity")
            cumulative = _decimal(row.get("cumulative_quantity", "0"), "open-order executed quantity")
            raw_limit_price = row.get("limit_price")
            limit_price = None if raw_limit_price is None else _decimal(raw_limit_price, "open-order limit price")
            if quantity <= 0 or cumulative < 0 or cumulative > quantity or (limit_price is not None and limit_price <= 0):
                raise CryptoComPortfolioError("Crypto.com open-order quantities or price are outside valid ranges.")
            orders.append(CryptoComOpenOrder(account_id, order_id, client_oid, instrument, side, order_type, status,
                quantity, cumulative, limit_price, timestamp(row.get("create_time"), "creation timestamp"),
                timestamp(row.get("update_time"), "update timestamp"), _OPEN_ORDERS_METHOD, digest))
        return tuple(orders)

    @staticmethod
    def _parse_positions(rows: list[object], digest: str) -> tuple[CryptoComPosition, ...]:
        positions: list[CryptoComPosition] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise CryptoComPortfolioError("Crypto.com returned an invalid position row.")
            instrument = row.get("instrument_name")
            position_type = row.get("type")
            if (not isinstance(instrument, str) or not _POSITION_INSTRUMENT.fullmatch(instrument)
                    or not isinstance(position_type, str) or not position_type):
                raise CryptoComPortfolioError("Crypto.com position identity is invalid.")
            raw_timestamp = row.get("update_timestamp_ms")
            try:
                if isinstance(raw_timestamp, str) and raw_timestamp.isdecimal():
                    raw_timestamp = int(raw_timestamp)
                if type(raw_timestamp) is not int or raw_timestamp < 0:
                    raise ValueError
                updated_at = datetime.fromtimestamp(raw_timestamp / 1000, timezone.utc)
            except (OverflowError, OSError, ValueError):
                raise CryptoComPortfolioError("Crypto.com position timestamp is invalid.") from None
            positions.append(CryptoComPosition(
                instrument_name=instrument,
                position_type=position_type,
                quantity=_decimal(row.get("quantity"), "position quantity"),
                cost_usd=_decimal(row.get("cost"), "position cost"),
                open_position_pnl_usd=_decimal(row.get("open_position_pnl"), "open position PnL"),
                session_pnl_usd=_decimal(row.get("session_pnl"), "session PnL"),
                updated_at=updated_at,
                source=_POSITIONS_METHOD,
                payload_sha256=digest,
            ))
        return tuple(positions)


__all__ = ["CryptoComBalance", "CryptoComBalanceHistory", "CryptoComBalanceHistoryPoint", "CryptoComExecutedTrade",
           "CryptoComFeeSchedule", "CryptoComFundingHistory", "CryptoComFundingHistoryPage", "CryptoComFundingTransfer",
           "CryptoComOpenOrder", "CryptoComPortfolioError", "CryptoComPosition", "CryptoComPortfolioSnapshot",
           "CryptoComTransaction", "CryptoComReadOnlyPortfolioClient", "CryptoComExchangeTransport"]
