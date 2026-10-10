"""Local Windows Hello/FIDO2 ceremonies for immutable Risk policy candidates.

Policy approval is separate from trading authority. This module never arms the
orchestrator, clears the kill switch, or connects an order adapter.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorTransport,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from invest_ai.core.clock import utc_now
from invest_ai.core.passkey_store import (
    PasskeyCredential,
    PasskeyStore,
    PasskeyStoreError,
    PolicyApprovalReceipt,
)
from invest_ai.core.risk_policy import RiskPolicyRegistry, RiskPolicyRegistryError

_CHALLENGE_TTL = timedelta(minutes=2)
_USER_ID = b"INVEST-AI-LOCAL-OWNER-V1"
_USER_NAME = "local-owner"
_HEX = re.compile(r"[0-9a-f]{64}\Z")


class PasskeyApprovalError(ValueError):
    """A local WebAuthn ceremony or policy approval failed closed."""


@dataclass(frozen=True, slots=True)
class RegistrationOptions:
    challenge_id: str
    public_key: dict[str, object]


@dataclass(frozen=True, slots=True)
class AuthenticationOptions:
    challenge_id: str
    revision_id: str
    public_key: dict[str, object]


def _instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PasskeyApprovalError("Une date UTC avec fuseau est obligatoire.")
    return value.astimezone(timezone.utc)


def _client_data(response: object) -> dict[str, object]:
    if not isinstance(response, dict) or not isinstance(response.get("response"), dict):
        raise PasskeyApprovalError("La réponse WebAuthn est invalide.")
    encoded = response["response"].get("clientDataJSON")
    if not isinstance(encoded, str) or len(encoded) > 16384:
        raise PasskeyApprovalError("Les données client WebAuthn sont absentes ou trop longues.")
    try:
        parsed = json.loads(base64url_to_bytes(encoded))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise PasskeyApprovalError("Les données client WebAuthn ne sont pas un JSON valide.") from None
    if (not isinstance(parsed, dict) or parsed.get("crossOrigin", False) is not False
            or parsed.get("type") not in {"webauthn.create", "webauthn.get"}
            or not isinstance(parsed.get("challenge"), str)
            or not isinstance(parsed.get("origin"), str)):
        raise PasskeyApprovalError("Contexte client WebAuthn inattendu.")
    return parsed


def _policy_is_complete(policy: object) -> bool:
    from invest_ai.risk import AssetType, RiskPolicy, Side

    if not isinstance(policy, RiskPolicy):
        return False
    if not isinstance(policy.version, str) or not policy.version.strip():
        return False
    if (not isinstance(policy.allowed_instruments, frozenset) or not policy.allowed_instruments
            or any(not isinstance(item, str) or not item.strip() for item in policy.allowed_instruments)
            or not isinstance(policy.allowed_sides, frozenset) or not policy.allowed_sides
            or any(not isinstance(item, Side) for item in policy.allowed_sides)
            or not isinstance(policy.allowed_asset_types, frozenset) or not policy.allowed_asset_types
            or any(not isinstance(item, AssetType) for item in policy.allowed_asset_types)):
        return False
    positive = ("max_order_notional", "max_position_notional", "max_gross_exposure",
                "max_loss_per_trade", "max_daily_loss", "max_leverage")
    ratios = ("max_concentration_ratio", "max_sector_concentration_ratio", "max_drawdown_ratio")
    for name in positive:
        value = getattr(policy, name)
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            return False
    for name in ratios:
        value = getattr(policy, name)
        if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1:
            return False
    for name in ("fee_rate", "slippage_buffer_rate", "min_cash_balance"):
        value = getattr(policy, name)
        if (not isinstance(value, Decimal) or not value.is_finite()
                or value < 0 or (name == "min_cash_balance" and value < 0)):
            return False
    for name in ("max_market_data_age_seconds", "max_portfolio_age_seconds",
                 "max_orders_per_window", "frequency_window_seconds"):
        value = getattr(policy, name)
        if type(value) is not int or value <= 0:
            return False
    return type(policy.require_stop_for_buy) is bool


class PasskeyApprovalService:
    """Verify local passkey enrollment and bind approval to exact trusted inputs."""

    def __init__(self, store: PasskeyStore, registry: RiskPolicyRegistry, readiness_provider,
                 *, rp_id: str, origin: str) -> None:
        if not isinstance(store, PasskeyStore) or not isinstance(registry, RiskPolicyRegistry):
            raise TypeError("PasskeyStore et RiskPolicyRegistry sont obligatoires.")
        if not callable(readiness_provider):
            raise TypeError("Un fournisseur local de readiness est obligatoire.")
        parsed = urlsplit(origin) if isinstance(origin, str) else None
        if (parsed is None or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or parsed.scheme not in {"http", "https"}
                or not parsed.hostname or parsed.hostname.casefold() != rp_id.casefold()
                or parsed.hostname.casefold() not in {"localhost", "127.0.0.1", "::1"}
                or origin != f"{parsed.scheme}://{parsed.netloc}"):
            raise PasskeyApprovalError("L’origine doit être locale/sécurisée et correspondre exactement au RP ID.")
        self._passkeys = store
        self._registry = registry
        self._readiness_provider = readiness_provider
        self._rp_id = rp_id
        self._origin = origin

    def begin_registration(self, *, now: datetime) -> RegistrationOptions:
        instant = _instant(now)
        if self._passkeys.credentials():
            raise PasskeyApprovalError("Une passkey existe déjà; l’inscription distante ou le reset sont interdits.")
        try:
            challenge = self._passkeys.create_challenge(
                purpose="register-passkey", context={"user_handle": bytes_to_base64url(_USER_ID)},
                expires_at=instant + _CHALLENGE_TTL,
            )
            options = generate_registration_options(
                rp_id=self._rp_id, rp_name="BUBUS2.0 INVEST-AI", user_id=_USER_ID,
                user_name=_USER_NAME, user_display_name="Propriétaire local INVEST-AI",
                challenge=base64url_to_bytes(challenge.challenge),
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=UserVerificationRequirement.REQUIRED,
                ),
            )
            return RegistrationOptions(challenge.challenge_id, json.loads(options_to_json(options)))
        except (PasskeyStoreError, ValueError, TypeError) as exc:
            raise PasskeyApprovalError("Impossible de préparer l’inscription passkey.") from exc

    def finish_registration(self, *, response: dict, now: datetime) -> PasskeyCredential:
        instant = _instant(now)
        try:
            client = _client_data(response)
            if client["type"] != "webauthn.create" or client["origin"] != self._origin:
                raise PasskeyApprovalError("Finalité ou origine WebAuthn incorrecte.")
            challenge = self._passkeys.challenge_by_value(str(client["challenge"]), purpose="register-passkey")
            if challenge is None or challenge.context != {"user_handle": bytes_to_base64url(_USER_ID)}:
                raise PasskeyApprovalError("Challenge d’inscription inconnu ou inattendu.")
            if challenge.expires_at <= instant:
                raise PasskeyApprovalError("Challenge d’inscription expiré.")
            verified = verify_registration_response(
                credential=response, expected_challenge=base64url_to_bytes(challenge.challenge),
                expected_rp_id=self._rp_id, expected_origin=self._origin,
                require_user_verification=True,
            )
            if verified.user_verified is not True:
                raise PasskeyApprovalError("Windows Hello/FIDO2 n’a pas vérifié l’utilisateur.")
            credential_id = bytes_to_base64url(verified.credential_id)
            transports = response.get("response", {}).get("transports", [])
            if not isinstance(transports, list) or any(not isinstance(item, str) for item in transports):
                raise PasskeyApprovalError("Transports WebAuthn invalides.")
            credential = PasskeyCredential(
                credential_id=credential_id, public_key=verified.credential_public_key,
                user_handle=bytes_to_base64url(_USER_ID), sign_count=verified.sign_count,
                transports=tuple(sorted(set(transports))), created_at=instant,
            )
            self._passkeys.complete_registration(
                challenge_id=challenge.challenge_id, now=instant, credential=credential,
            )
            return credential
        except PasskeyApprovalError:
            raise
        except Exception as exc:
            raise PasskeyApprovalError("La vérification ou l’enregistrement passkey a échoué.") from exc

    def begin_policy_approval(self, *, revision_id: str, now: datetime) -> AuthenticationOptions:
        instant = _instant(now)
        credentials = self._passkeys.credentials()
        if not credentials:
            raise PasskeyApprovalError("Aucune passkey locale n’est enregistrée.")
        try:
            candidate = self._registry.candidate(revision_id)
            if candidate is None or not _policy_is_complete(candidate[1]):
                raise PasskeyApprovalError("La politique complète candidate est absente ou invalide.")
            readiness = self._readiness_document()
            control = self._passkeys._store.control_state()
            if control.halted:
                raise PasskeyApprovalError("Le kill switch doit être clair pour approuver la politique.")
            context = {
                "revision_id": revision_id,
                "policy_digest": candidate[0].policy_digest,
                "readiness_digest": _digest(readiness),
                "control_epoch": control.control_epoch,
            }
            challenge = self._passkeys.create_challenge(
                purpose="approve-risk-policy", context=context,
                expires_at=instant + _CHALLENGE_TTL,
            )
            options = generate_authentication_options(
                rp_id=self._rp_id, challenge=base64url_to_bytes(challenge.challenge),
                allow_credentials=[_credential_descriptor(item) for item in credentials],
                user_verification=UserVerificationRequirement.REQUIRED,
            )
            return AuthenticationOptions(challenge.challenge_id, revision_id,
                                         json.loads(options_to_json(options)))
        except PasskeyApprovalError:
            raise
        except (PasskeyStoreError, RiskPolicyRegistryError, ValueError, TypeError) as exc:
            raise PasskeyApprovalError("Impossible de préparer l’approbation Risk.") from exc

    def finish_policy_approval(self, *, response: dict, now: datetime) -> PolicyApprovalReceipt:
        instant = _instant(now)
        try:
            client = _client_data(response)
            if client["type"] != "webauthn.get" or client["origin"] != self._origin:
                raise PasskeyApprovalError("Finalité ou origine WebAuthn incorrecte.")
            challenge = self._passkeys.challenge_by_value(str(client["challenge"]), purpose="approve-risk-policy")
            if challenge is None or challenge.expires_at <= instant:
                raise PasskeyApprovalError("Challenge d’approbation absent ou expiré.")
            context = challenge.context
            if set(context) != {"revision_id", "policy_digest", "readiness_digest", "control_epoch"}:
                raise PasskeyApprovalError("Contexte d’approbation incomplet.")
            if type(context["control_epoch"]) is not int:
                raise PasskeyApprovalError("Epoch de contrôle invalide.")
            candidate = self._registry.candidate(context["revision_id"])
            if candidate is None or not _policy_is_complete(candidate[1]):
                raise PasskeyApprovalError("La politique candidate n’est plus éligible.")
            readiness = self._readiness_document()
            control = self._passkeys._store.control_state()
            if (candidate[0].policy_digest != context["policy_digest"]
                    or _digest(readiness) != context["readiness_digest"]
                    or control.control_epoch != context["control_epoch"] or control.halted):
                raise PasskeyApprovalError("Politique, readiness ou contrôle modifié depuis la demande.")
            credential_id = response.get("id")
            if not isinstance(credential_id, str):
                raise PasskeyApprovalError("Identifiant de passkey absent.")
            credential = self._passkeys.credential(credential_id)
            if credential is None:
                raise PasskeyApprovalError("Passkey inconnue.")
            user_handle = response.get("response", {}).get("userHandle")
            if user_handle is not None and user_handle != credential.user_handle:
                raise PasskeyApprovalError("La passkey ne correspond pas au propriétaire local.")
            verified = verify_authentication_response(
                credential=response, expected_challenge=base64url_to_bytes(challenge.challenge),
                expected_rp_id=self._rp_id, expected_origin=self._origin,
                credential_public_key=credential.public_key,
                credential_current_sign_count=credential.sign_count,
                require_user_verification=True,
            )
            if verified.user_verified is not True:
                raise PasskeyApprovalError("La vérification utilisateur WebAuthn est obligatoire.")
            assertion_json = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            receipt = PolicyApprovalReceipt.issue(
                revision_id=context["revision_id"], policy_digest=context["policy_digest"],
                readiness_digest=context["readiness_digest"], control_epoch=context["control_epoch"],
                credential_id=credential.credential_id, challenge_id=challenge.challenge_id,
                assertion_digest=hashlib.sha256(assertion_json.encode("utf-8")).hexdigest(),
                recorded_at=instant,
            )
            self._passkeys.complete_approval(
                receipt=receipt, sign_count=verified.new_sign_count, now=instant,
            )
            return receipt
        except PasskeyApprovalError:
            raise
        except Exception as exc:
            raise PasskeyApprovalError("L’assertion ou l’approbation Risk a échoué.") from exc

    def _readiness_document(self) -> dict[str, object]:
        value = self._readiness_provider()
        if (not isinstance(value, dict) or value.get("armed") is not False
                or value.get("orders_enabled") is not False):
            raise PasskeyApprovalError("La readiness locale doit confirmer que l’exécution reste désactivée.")
        try:
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise PasskeyApprovalError("La readiness locale n’est pas canonique.") from None
        return value


def _digest(value: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _credential_descriptor(credential: PasskeyCredential) -> PublicKeyCredentialDescriptor:
    """Convert persisted base64url IDs into the byte-based py_webauthn API."""
    try:
        credential_id = base64url_to_bytes(credential.credential_id)
    except (TypeError, ValueError) as exc:
        raise PasskeyApprovalError("L’identifiant stocké de la passkey est invalide.") from exc
    if bytes_to_base64url(credential_id) != credential.credential_id:
        raise PasskeyApprovalError("L’identifiant stocké de la passkey n’est pas du base64url canonique.")
    known_transports = []
    for value in credential.transports:
        try:
            known_transports.append(AuthenticatorTransport(value))
        except ValueError:
            # Transports are hints. Unknown values must not prevent a valid
            # registered credential from being selected by the authenticator.
            continue
    return PublicKeyCredentialDescriptor(
        id=credential_id,
        transports=known_transports or None,
    )
