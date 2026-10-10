"""Balance-only observer for Crypto.com App Agent Key credentials.

This is intentionally separate from the Crypto.com Exchange API adapter. It
supports exactly two authenticated GET routes and has no trading or cash APIs.
Credentials live only in the current request and are never persisted.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Protocol


_BASE_URL = "https://wapi.crypto.com"
_FIAT_PATH = "/v1/fiat-account"
_CRYPTO_PATH = "/v1/crypto-account"
_ALLOWED_PATHS = frozenset({_FIAT_PATH, _CRYPTO_PATH})
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_CURRENCY = re.compile(r"[A-Z0-9]{2,20}\Z")


class CryptoComAppPortfolioError(ValueError):
    """Invalid, unauthorized, or unavailable read-only App balance snapshot."""


class JsonTransport(Protocol):
    def get_json(self, path: str, api_key: str, api_secret: str) -> tuple[object, bytes]: ...


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _safe_http_error(exc: urllib.error.HTTPError) -> CryptoComAppPortfolioError:
    status = exc.code
    api_code = None
    try:
        raw = exc.read(64 * 1024 + 1)
        if len(raw) <= 64 * 1024:
            payload = json.loads(raw)
            candidate = payload.get("code") if isinstance(payload, Mapping) else None
            if type(candidate) is int:
                api_code = candidate
    except Exception:
        pass
    finally:
        exc.close()
    detail = f", code API {api_code}" if api_code is not None else ""
    return CryptoComAppPortfolioError(f"Crypto.com App a répondu HTTP {status}{detail}.")


class CryptoComAppTransport:
    """Verified TLS transport restricted to the two App balance GET endpoints."""

    def get_json(self, path: str, api_key: str, api_secret: str) -> tuple[object, bytes]:
        if path not in _ALLOWED_PATHS:
            raise CryptoComAppPortfolioError("Crypto.com App route is not allowlisted.")
        if not api_key or not api_secret:
            raise CryptoComAppPortfolioError("Crypto.com App API key and secret are required.")
        timestamp = str(time.time_ns() // 1_000_000)
        sign_payload = f"{timestamp}GET{path}"
        signature = base64.b64encode(
            hmac.new(api_secret.encode("utf-8"), sign_payload.encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii")
        request = urllib.request.Request(
            f"{_BASE_URL}{path}", method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "INVEST-AI-read-only/1.0",
                "Cdc-Api-Key": api_key,
                "Cdc-Api-Timestamp": timestamp,
                "Cdc-Api-Signature": signature,
            },
        )
        try:
            context = ssl.create_default_context()
            if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                raise CryptoComAppPortfolioError("Crypto.com TLS verification must remain enabled.")
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
                _RejectRedirectHandler(),
            )
            response = opener.open(request, timeout=8)
            with response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except CryptoComAppPortfolioError:
            raise
        except urllib.error.HTTPError as exc:
            raise _safe_http_error(exc) from None
        except Exception:
            raise CryptoComAppPortfolioError("Crypto.com App connection failed or TLS verification failed.") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise CryptoComAppPortfolioError("Crypto.com App response exceeded the configured size limit.")
        try:
            payload = json.loads(raw, parse_float=Decimal, parse_int=int, parse_constant=_reject_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise CryptoComAppPortfolioError("Crypto.com App returned invalid JSON.") from None
        return payload, raw


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON number: {value}")


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, (float, bool)) or value is None:
        raise CryptoComAppPortfolioError(f"Invalid {field} in Crypto.com App response.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)  # type: ignore[arg-type]
    except (InvalidOperation, TypeError, ValueError):
        raise CryptoComAppPortfolioError(f"Invalid {field} in Crypto.com App response.") from None
    if not result.is_finite():
        raise CryptoComAppPortfolioError(f"Non-finite {field} in Crypto.com App response.")
    return result


def _amount(value: object, field: str) -> Decimal:
    if isinstance(value, Mapping):
        value = value.get("amount")
    return _decimal(value, field)


def _currency(value: object) -> str:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise CryptoComAppPortfolioError("Invalid currency in Crypto.com App response.")
    return value


def _account_data(payload: object, label: str) -> Mapping:
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        raise CryptoComAppPortfolioError(f"Crypto.com App rejected the {label} balance read.")
    account = payload.get("account")
    if not isinstance(account, Mapping):
        raise CryptoComAppPortfolioError(f"Crypto.com App returned no {label} account balances.")
    return account


@dataclass(frozen=True, slots=True)
class CryptoComAppBalance:
    currency: str
    account_type: str
    total: Decimal
    available: Decimal
    source: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class CryptoComAppPortfolioSnapshot:
    fetched_at: datetime
    balances: tuple[CryptoComAppBalance, ...]
    payload_sha256: Mapping[str, str]
    provider: str = "Crypto.com App"
    authority_status: str = "BROKER_READ_ONLY_OBSERVATION_ONLY"
    persistent: bool = False
    usable_for_risk: bool = False
    usable_for_execution: bool = False


class CryptoComAppReadOnlyClient:
    """Read fiat and crypto balances using only GET calls for one user click."""

    def __init__(self, transport: JsonTransport | None = None, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._transport = transport or CryptoComAppTransport()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def read_balances(self, api_key: str, api_secret: str) -> CryptoComAppPortfolioSnapshot:
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 512:
            raise CryptoComAppPortfolioError("Enter a valid Crypto.com App Agent Key.")
        if not isinstance(api_secret, str) or not api_secret or len(api_secret) > 1024:
            raise CryptoComAppPortfolioError("Enter the matching Crypto.com App secret.")
        observations: list[CryptoComAppBalance] = []
        digests: dict[str, str] = {}
        for path, label in ((_FIAT_PATH, "fiat"), (_CRYPTO_PATH, "crypto")):
            try:
                payload, raw = self._transport.get_json(path, api_key, api_secret)
            except urllib.error.HTTPError as exc:
                raise _safe_http_error(exc) from None
            digest = hashlib.sha256(raw).hexdigest()
            digests[label] = digest
            account = _account_data(payload, label)
            if label == "fiat":
                rows = account.get("balances")
                if not isinstance(rows, list):
                    raise CryptoComAppPortfolioError("Crypto.com App returned an invalid fiat balance list.")
                for row in rows:
                    if not isinstance(row, Mapping):
                        raise CryptoComAppPortfolioError("Crypto.com App returned an invalid fiat balance row.")
                    amount = _amount(row.get("amount"), "fiat amount")
                    if amount == 0:
                        continue
                    observations.append(CryptoComAppBalance(
                        currency=_currency(row.get("currency")), account_type="FIAT",
                        total=amount, available=amount, source=_FIAT_PATH, payload_sha256=digest,
                    ))
            else:
                rows = account.get("wallets")
                if not isinstance(rows, list):
                    raise CryptoComAppPortfolioError("Crypto.com App returned an invalid crypto wallet list.")
                for row in rows:
                    if not isinstance(row, Mapping):
                        raise CryptoComAppPortfolioError("Crypto.com App returned an invalid crypto wallet row.")
                    available_value = row.get("available")
                    total_value = row.get("balance", available_value)
                    available = _amount(available_value if available_value is not None else total_value,
                                        "crypto available balance")
                    total = _amount(total_value, "crypto total balance")
                    if total == 0 and available == 0:
                        continue
                    observations.append(CryptoComAppBalance(
                        currency=_currency(row.get("currency")), account_type="CRYPTO",
                        total=total, available=available, source=_CRYPTO_PATH, payload_sha256=digest,
                    ))
        if not observations:
            raise CryptoComAppPortfolioError("Crypto.com App returned no non-zero fiat or crypto balances.")
        fetched_at = self._clock()
        if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None:
            raise CryptoComAppPortfolioError("Local collection timestamp must be timezone-aware.")
        return CryptoComAppPortfolioSnapshot(
            fetched_at=fetched_at.astimezone(timezone.utc), balances=tuple(observations), payload_sha256=digests,
        )


__all__ = ["CryptoComAppBalance", "CryptoComAppPortfolioError", "CryptoComAppPortfolioSnapshot",
           "CryptoComAppReadOnlyClient", "CryptoComAppTransport"]
