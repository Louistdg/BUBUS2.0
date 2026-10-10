"""Explicitly gated Crypto.com Exchange spot limit-order adapter.

This adapter is intentionally not wired to the local dashboard. A production
caller must supply a durable order journal and trusted, single-use
Risk/Policy/human-approval gate. The default configuration has neither and
cannot send an order.
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
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Protocol


_BASE_URL = "https://api.crypto.com/exchange/v1"
_CREATE_ORDER = "private/create-order"
_ORDER_DETAIL = "private/get-order-detail"
_SPOT_PAIR = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,20}_[A-Z0-9]{2,12}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ORDER_STATUSES = frozenset({"PENDING", "NEW", "ACTIVE", "FILLED", "CANCELED", "REJECTED", "EXPIRED"})
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_DECIMAL_DIGITS = 40


class CryptoComSpotOrderError(ValueError):
    """An order was not submitted or its exchange state could not be confirmed."""


class OrderAuthorizationGate(Protocol):
    """Trusted Core hook: atomically consume a current, exact-intent approval."""

    def consume(self, intent: "CryptoComSpotLimitOrder", intent_sha256: str) -> "OrderAuthorization": ...


class SpotInstrumentVerifier(Protocol):
    """Verify current Exchange metadata identifies a live, tradable SPOT pair."""

    def assert_spot_limit_order(self, intent: "CryptoComSpotLimitOrder") -> None: ...


class PrivateTransport(Protocol):
    def request(self, method: str, api_key: str, api_secret: str,
                params: Mapping[str, str]) -> Mapping[str, object]: ...


class DurableOrderJournal(Protocol):
    """Crash-persistent intent and submission evidence required by the adapter."""

    def reserve_intent(self, intent: "CryptoComSpotLimitOrder") -> None: ...
    def record_authorization(self, client_oid: str, *, risk_receipt_id: str,
                             policy_revision: str, control_epoch: int,
                             human_approval_id: str) -> None: ...
    def record_authorization_rejected(self, client_oid: str, reason_code: str) -> None: ...
    def record_submission_attempt(self, client_oid: str, intent_sha256: str) -> None: ...
    def record_submission_uncertain(self, client_oid: str, reason_code: str) -> None: ...
    def record_acknowledgement(self, client_oid: str, *, order_id: str,
                               echoed_client_oid: str) -> None: ...
    def contains_intent(self, client_oid: str) -> bool: ...
    def record_order_readback(self, status: "CryptoComOrderStatus") -> None: ...


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CryptoComSpotOrderError(f"{name} must be a finite Decimal; floats are forbidden.")
    parts = value.as_tuple()
    if len(parts.digits) > _MAX_DECIMAL_DIGITS or abs(parts.exponent) > _MAX_DECIMAL_DIGITS:
        raise CryptoComSpotOrderError(f"{name} exceeds the supported Decimal bounds.")
    if positive and value <= 0:
        raise CryptoComSpotOrderError(f"{name} must be greater than zero.")
    return value


@dataclass(frozen=True, slots=True)
class CryptoComSpotLimitOrder:
    """One explicitly reviewed Spot LIMIT order; no derivatives or market order."""

    client_oid: str
    instrument_name: str
    side: str
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        try:
            parsed_oid = uuid.UUID(self.client_oid)
        except (ValueError, TypeError, AttributeError):
            raise CryptoComSpotOrderError("client_oid must be a UUID for exchange idempotence.") from None
        if str(parsed_oid) != self.client_oid:
            raise CryptoComSpotOrderError("client_oid must use canonical UUID form.")
        if not isinstance(self.instrument_name, str) or not _SPOT_PAIR.fullmatch(self.instrument_name):
            raise CryptoComSpotOrderError("Only a valid Crypto.com Spot pair such as BTC_USDT is allowed.")
        if self.side not in {"BUY", "SELL"}:
            raise CryptoComSpotOrderError("side must be BUY or SELL.")
        _decimal(self.price, "price", positive=True)
        _decimal(self.quantity, "quantity", positive=True)

    def canonical_sha256(self) -> str:
        payload = {
            "client_oid": self.client_oid,
            "instrument_name": self.instrument_name,
            "order_type": "LIMIT",
            "price": format(self.price, "f"),
            "quantity": format(self.quantity, "f"),
            "side": self.side,
            "time_in_force": "FILL_OR_KILL",
        }
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class OrderAuthorization:
    """Receipt returned by the trusted gate immediately before exchange submission."""

    risk_receipt_id: str
    policy_revision: str
    control_epoch: int
    human_approval_id: str
    intent_sha256: str
    expires_at: datetime

    def __post_init__(self) -> None:
        for name in ("risk_receipt_id", "policy_revision", "human_approval_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise CryptoComSpotOrderError(f"{name} is required by the order gate.")
        if type(self.control_epoch) is not int or self.control_epoch < 0:
            raise CryptoComSpotOrderError("A nonnegative current control_epoch is required.")
        if not isinstance(self.intent_sha256, str) or not _SHA256.fullmatch(self.intent_sha256):
            raise CryptoComSpotOrderError("The order gate returned an invalid intent digest.")
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise CryptoComSpotOrderError("The order approval must have an aware expiry time.")
        if self.expires_at.utcoffset() is None:
            raise CryptoComSpotOrderError("The order approval must have a valid timezone offset.")


@dataclass(frozen=True, slots=True)
class CryptoComOrderAcknowledgement:
    """Exchange accepted the request; this is not proof of a fill."""

    order_id: str
    client_oid: str
    status: str = "ACKNOWLEDGED_NOT_FILLED"
    intent_sha256: str = ""
    risk_receipt_id: str = ""
    policy_revision: str = ""
    control_epoch: int = 0
    human_approval_id: str = ""


@dataclass(frozen=True, slots=True)
class CryptoComOrderStatus:
    order_id: str
    client_oid: str
    status: str
    executed_quantity: Decimal
    executed_value: Decimal
    fees: Decimal
    updated_at: datetime | None
    fee_currency: str | None = None


class CryptoComExchangePrivateTransport:
    """Verified-TLS transport pinned to create-order and get-order-detail."""

    def __init__(self, *, nonce: Callable[[], int] | None = None,
                 request_id: Callable[[], int] | None = None) -> None:
        self._nonce = nonce or (lambda: time.time_ns() // 1_000_000)
        self._request_id = request_id or (lambda: time.time_ns() // 1_000_000)
        self._lock = threading.Lock()
        self._last_nonce = 0
        self._last_id = 0

    def request(self, method: str, api_key: str, api_secret: str,
                params: Mapping[str, str]) -> Mapping[str, object]:
        if method not in {_CREATE_ORDER, _ORDER_DETAIL}:
            raise CryptoComSpotOrderError("Crypto.com private method is not enabled by the Spot adapter.")
        _validate_credentials(api_key, api_secret)
        if not isinstance(params, Mapping) or any(not isinstance(k, str) or not isinstance(v, str)
                                                  for k, v in params.items()):
            raise CryptoComSpotOrderError("Crypto.com private request parameters are invalid.")
        with self._lock:
            nonce = self._next(self._nonce, self._last_nonce)
            request_id = self._next(self._request_id, self._last_id)
            self._last_nonce, self._last_id = nonce, request_id
        params_dict = dict(params)
        payload = method + str(request_id) + api_key + _params_string(params_dict) + str(nonce)
        signature = hmac.new(api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        body = json.dumps({
            "id": request_id, "method": method, "api_key": api_key,
            "params": params_dict, "nonce": nonce, "sig": signature,
        }, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{_BASE_URL}/{method}", data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            context = ssl.create_default_context()
            if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                raise CryptoComSpotOrderError("Crypto.com TLS verification must remain enabled.")
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context),
                _RejectRedirectHandler(),
            )
            response = opener.open(request, timeout=8)
            with response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except CryptoComSpotOrderError:
            raise
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise CryptoComSpotOrderError(
                f"Crypto.com returned HTTP {status}; submission state must be reconciled before retrying."
            ) from None
        except Exception:
            raise CryptoComSpotOrderError(
                "Crypto.com response unavailable; submission state is unknown and must be reconciled before retrying."
            ) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise CryptoComSpotOrderError("Crypto.com response exceeded the configured size limit.")
        try:
            decoded = json.loads(raw, parse_float=Decimal, parse_int=int,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError, UnicodeDecodeError):
            raise CryptoComSpotOrderError("Crypto.com returned invalid JSON; reconcile before retrying.") from None
        if not isinstance(decoded, Mapping):
            raise CryptoComSpotOrderError("Crypto.com returned an invalid response envelope.")
        if decoded.get("id") != request_id or decoded.get("method") != method:
            raise CryptoComSpotOrderError("Crypto.com response does not match the requested method/id.")
        code = decoded.get("code")
        if type(code) is not int or code != 0:
            raise CryptoComSpotOrderError(
                f"Crypto.com refused the private request (API code {code if type(code) is int else 'invalid'})."
            )
        return decoded

    @staticmethod
    def _next(source: Callable[[], int], previous: int) -> int:
        value = source()
        if type(value) is not int or value <= 0:
            raise CryptoComSpotOrderError("Unable to create a valid Crypto.com request id/nonce.")
        return max(value, previous + 1)


class CryptoComSpotOrderAdapter:
    """Spot LIMIT adapter. No authorization gate means no network order call."""

    def __init__(self, *, authorization_gate: OrderAuthorizationGate | None = None,
                 instrument_verifier: SpotInstrumentVerifier | None = None,
                 order_journal: DurableOrderJournal | None = None,
                 transport: PrivateTransport | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._gate = authorization_gate
        self._instrument_verifier = instrument_verifier
        self._order_journal = order_journal
        self._transport = transport or CryptoComExchangePrivateTransport()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Additional in-process brake; the durable journal owns cross-restart
        # uniqueness and the gate must still atomically reserve exposure.
        self._attempt_lock = threading.Lock()
        self._attempted_oids: dict[str, str] = {}

    def create_limit_order(self, intent: CryptoComSpotLimitOrder, *, api_key: str,
                           api_secret: str) -> CryptoComOrderAcknowledgement:
        if not isinstance(intent, CryptoComSpotLimitOrder):
            raise CryptoComSpotOrderError("A validated Crypto.com Spot LIMIT intent is required.")
        _validate_credentials(api_key, api_secret)
        if self._instrument_verifier is None:
            raise CryptoComSpotOrderError(
                "Real orders are disabled: current Crypto.com SPOT instrument rules are not verified."
            )
        try:
            self._instrument_verifier.assert_spot_limit_order(intent)
        except Exception:
            raise CryptoComSpotOrderError("Crypto.com did not confirm this as a tradable SPOT instrument.") from None
        if self._gate is None:
            raise CryptoComSpotOrderError(
                "Real orders are disabled: no trusted Risk/Policy/human-approval gate is connected."
            )
        if self._order_journal is None:
            raise CryptoComSpotOrderError(
                "Real orders are disabled: a durable cross-restart order journal is not connected."
            )
        digest = intent.canonical_sha256()
        # Reserve before consuming approval: two local threads must not attempt
        # the same client_oid, even if the first submission times out.
        # This is NOT crash-proof; an independent durable Core ledger remains
        # mandatory before attaching a real authorization gate.
        with self._attempt_lock:
            if intent.client_oid in self._attempted_oids:
                raise CryptoComSpotOrderError(
                    "client_oid already attempted in this process; reconcile by client_oid, never retry blindly."
                )
            self._attempted_oids[intent.client_oid] = digest
        try:
            self._order_journal.reserve_intent(intent)
        except Exception as exc:
            raise CryptoComSpotOrderError(
                f"Durable order intent reservation failed; do not retry blindly: {exc}"
            ) from None
        try:
            authorization = self._gate.consume(intent, digest)
        except Exception:
            self._record_authorization_rejected(intent.client_oid, "GATE_REFUSED")
            raise CryptoComSpotOrderError("The trusted order gate refused or could not consume approval.") from None
        if not isinstance(authorization, OrderAuthorization):
            self._record_authorization_rejected(intent.client_oid, "INVALID_GATE_RECEIPT")
            raise CryptoComSpotOrderError("The trusted order gate returned no valid authorization receipt.")
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            self._record_authorization_rejected(intent.client_oid, "INVALID_LOCAL_CLOCK")
            raise CryptoComSpotOrderError("A valid local UTC clock is required before order submission.")
        if authorization.intent_sha256 != digest or authorization.expires_at <= now.astimezone(timezone.utc):
            self._record_authorization_rejected(intent.client_oid, "APPROVAL_MISMATCH_OR_EXPIRED")
            raise CryptoComSpotOrderError("The exact-intent order approval is mismatched or expired.")
        try:
            self._order_journal.record_authorization(
                intent.client_oid, risk_receipt_id=authorization.risk_receipt_id,
                policy_revision=authorization.policy_revision, control_epoch=authorization.control_epoch,
                human_approval_id=authorization.human_approval_id,
            )
            self._order_journal.record_submission_attempt(intent.client_oid, digest)
        except Exception:
            raise CryptoComSpotOrderError(
                "Durable authorization or attempt record failed; no exchange request was sent."
            ) from None
        params = {
            "instrument_name": intent.instrument_name,
            "side": intent.side,
            "type": "LIMIT",
            "price": format(intent.price, "f"),
            "quantity": format(intent.quantity, "f"),
            "client_oid": intent.client_oid,
            "time_in_force": "FILL_OR_KILL",
        }
        try:
            response = self._transport.request(_CREATE_ORDER, api_key, api_secret, params)
        except Exception:
            self._mark_submission_uncertain(intent.client_oid, "TRANSPORT_ERROR")
            raise CryptoComSpotOrderError(
                "Submission response unknown; reconcile by client_oid before any new order intent."
            ) from None
        if (not isinstance(response, Mapping) or response.get("method") != _CREATE_ORDER
                or type(response.get("code")) is not int or response["code"] != 0):
            self._mark_submission_uncertain(intent.client_oid, "INVALID_EXCHANGE_ACK")
            raise CryptoComSpotOrderError("Crypto.com create-order response is not a matching successful acknowledgement.")
        result = response.get("result")
        if not isinstance(result, Mapping):
            self._mark_submission_uncertain(intent.client_oid, "MISSING_EXCHANGE_ORDER_ID")
            raise CryptoComSpotOrderError("Crypto.com returned no order id; reconcile by client_oid before retrying.")
        order_id, client_oid = result.get("order_id"), result.get("client_oid")
        if (not isinstance(order_id, (str, int)) or isinstance(order_id, bool)
                or re.fullmatch(r"[0-9]{1,32}", str(order_id)) is None or int(order_id) <= 0):
            self._mark_submission_uncertain(intent.client_oid, "INVALID_EXCHANGE_ORDER_ID")
            raise CryptoComSpotOrderError("Crypto.com returned an invalid order id; reconcile before retrying.")
        if client_oid != intent.client_oid:
            self._mark_submission_uncertain(intent.client_oid, "CLIENT_OID_MISMATCH")
            raise CryptoComSpotOrderError("Crypto.com echoed a different client_oid; reconcile before retrying.")
        try:
            self._order_journal.record_acknowledgement(
                intent.client_oid, order_id=str(order_id), echoed_client_oid=client_oid,
            )
        except Exception:
            raise CryptoComSpotOrderError(
                "Crypto.com may have accepted the order, but durable acknowledgement failed; reconcile before retrying."
            ) from None
        return CryptoComOrderAcknowledgement(
            order_id=str(order_id), client_oid=client_oid, intent_sha256=digest,
            risk_receipt_id=authorization.risk_receipt_id,
            policy_revision=authorization.policy_revision,
            control_epoch=authorization.control_epoch,
            human_approval_id=authorization.human_approval_id,
        )

    def _record_authorization_rejected(self, client_oid: str, reason_code: str) -> None:
        try:
            if self._order_journal is not None:
                self._order_journal.record_authorization_rejected(client_oid, reason_code)
        except Exception:
            # Keep the adapter fail-closed if audit persistence is unavailable.
            pass

    def _mark_submission_uncertain(self, client_oid: str, reason_code: str) -> None:
        try:
            if self._order_journal is not None:
                self._order_journal.record_submission_uncertain(client_oid, reason_code)
        except Exception:
            # The process-local duplicate brake remains active; restart recovery
            # is blocked by the durable ATTEMPTED state if the write failed.
            pass

    def get_order_detail(self, *, api_key: str, api_secret: str,
                         order_id: str | None = None,
                         client_oid: str | None = None) -> CryptoComOrderStatus:
        """Read the result by ID or client_oid, including after an uncertain submit."""
        if (order_id is None) == (client_oid is None):
            raise CryptoComSpotOrderError("Supply exactly one of order_id or client_oid.")
        if order_id is not None and (not isinstance(order_id, str) or not re.fullmatch(r"[0-9]{1,32}", order_id)):
            raise CryptoComSpotOrderError("order_id must be a string of digits.")
        if client_oid is not None:
            try:
                if str(uuid.UUID(client_oid)) != client_oid:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise CryptoComSpotOrderError("client_oid must be a canonical UUID.") from None
        requested_client_oid = client_oid
        _validate_credentials(api_key, api_secret)
        lookup = {"order_id": order_id} if order_id is not None else {"client_oid": client_oid}
        try:
            response = self._transport.request(_ORDER_DETAIL, api_key, api_secret, lookup)
        except CryptoComSpotOrderError:
            raise
        except Exception:
            raise CryptoComSpotOrderError(
                "Order detail unavailable; absence of a response is not proof that no order exists."
            ) from None
        if (not isinstance(response, Mapping) or response.get("method") != _ORDER_DETAIL
                or type(response.get("code")) is not int or response["code"] != 0):
            raise CryptoComSpotOrderError("Crypto.com order-detail response is not a matching successful result.")
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise CryptoComSpotOrderError("Crypto.com returned no order detail.")
        row = result
        returned_id = row.get("order_id")
        if (not isinstance(returned_id, (str, int)) or isinstance(returned_id, bool)
                or re.fullmatch(r"[0-9]{1,32}", str(returned_id)) is None
                or int(returned_id) <= 0):
            raise CryptoComSpotOrderError("Crypto.com order detail has no valid order id.")
        if order_id is not None and str(returned_id) != order_id:
            raise CryptoComSpotOrderError("Crypto.com order detail id does not match the request.")
        client_oid, status = row.get("client_oid"), row.get("status")
        if (not isinstance(client_oid, str)
                or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", client_oid) is None
                or not isinstance(status, str) or status not in _ORDER_STATUSES):
            raise CryptoComSpotOrderError("Crypto.com order detail identity/status is unrecognized.")
        if requested_client_oid is not None and client_oid != requested_client_oid:
            raise CryptoComSpotOrderError("Crypto.com order detail client_oid does not match the request.")
        fee_currency = row.get("fee_instrument_name")
        if (fee_currency is not None and
                (not isinstance(fee_currency, str)
                 or re.fullmatch(r"[A-Z0-9]{2,16}", fee_currency) is None)):
            raise CryptoComSpotOrderError("Crypto.com fee currency is malformed.")
        try:
            executed_quantity = _response_decimal(row.get("cumulative_quantity"), "cumulative_quantity")
            executed_value = _response_decimal(row.get("cumulative_value"), "cumulative_value")
            fees = _response_decimal(row.get("cumulative_fee"), "cumulative_fee")
            update_time = row.get("update_time")
            if update_time is None:
                updated_at = None  # Unknown timestamp: never infer freshness.
            else:
                if ((type(update_time) is not int and
                     not (isinstance(update_time, str) and
                          re.fullmatch(r"[0-9]{1,16}", update_time)))
                        or int(update_time) <= 0):
                    raise ValueError("invalid update_time")
                updated_at = datetime.fromtimestamp(int(update_time) / 1000, timezone.utc)
        except (InvalidOperation, TypeError, ValueError, OverflowError, OSError):
            raise CryptoComSpotOrderError("Crypto.com order detail contains invalid execution values.") from None
        if updated_at is not None:
            now = self._clock()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise CryptoComSpotOrderError("Order readback needs a valid local UTC clock.")
            if updated_at > now.astimezone(timezone.utc) + timedelta(seconds=5):
                raise CryptoComSpotOrderError("Crypto.com order update timestamp is in the future.")
        if status == "FILLED" and executed_quantity <= 0:
            raise CryptoComSpotOrderError("Crypto.com FILLED status cannot have zero executed quantity.")
        parsed_status = CryptoComOrderStatus(str(returned_id), client_oid, status, executed_quantity,
                                             executed_value, fees, updated_at, fee_currency)
        try:
            if self._order_journal is not None and self._order_journal.contains_intent(client_oid):
                self._order_journal.record_order_readback(parsed_status)
        except Exception:
            raise CryptoComSpotOrderError(
                "Crypto.com order detail was received but durable readback recording failed."
            ) from None
        return parsed_status


def _validate_credentials(api_key: str, api_secret: str) -> None:
    if (not isinstance(api_key, str) or not api_key or len(api_key) > 256 or "\n" in api_key
            or not isinstance(api_secret, str) or not api_secret or len(api_secret) > 512
            or "\n" in api_secret):
        raise CryptoComSpotOrderError("Valid Crypto.com Exchange API credentials are required.")


def _params_string(params: Mapping[str, str]) -> str:
    # All currently enabled order fields are scalar strings; do not silently
    # invent signature rules for nested/array parameters.
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in params.items()):
        raise CryptoComSpotOrderError("Only scalar string parameters are supported by this signer.")
    return "".join(key + params[key] for key in sorted(params))


def _response_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise InvalidOperation
    if isinstance(value, str) and len(value) > 80:
        raise InvalidOperation
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise InvalidOperation
    parts = result.as_tuple()
    if len(parts.digits) > _MAX_DECIMAL_DIGITS or abs(parts.exponent) > _MAX_DECIMAL_DIGITS:
        raise InvalidOperation
    return result


__all__ = [
    "CryptoComOrderAcknowledgement", "CryptoComOrderStatus", "CryptoComSpotLimitOrder",
    "CryptoComSpotOrderAdapter", "CryptoComSpotOrderError", "OrderAuthorization",
    "OrderAuthorizationGate", "PrivateTransport", "SpotInstrumentVerifier",
]
