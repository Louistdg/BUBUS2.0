"""Durable local WebAuthn credentials, one-use challenges, and approval receipts."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from invest_ai.core.clock import iso_utc, parse_utc, to_utc, utc_now
from invest_ai.core.ids import new_id
from invest_ai.core.orchestration import OrchestrationStore, _hash

_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CREDENTIAL_ID = re.compile(r"[A-Za-z0-9_-]{1,1024}\Z")
_PURPOSES = frozenset({"register-passkey", "approve-risk-policy"})
_MAX_CHALLENGE_AGE = timedelta(minutes=2)


class PasskeyStoreError(ValueError):
    """Stored WebAuthn state is invalid, expired, replayed, or inconsistent."""


@dataclass(frozen=True, slots=True)
class WebAuthnChallenge:
    challenge_id: str
    challenge: str
    purpose: str
    context_json: str
    created_at: datetime
    expires_at: datetime

    @property
    def context(self) -> dict[str, Any]:
        value = json.loads(self.context_json)
        if not isinstance(value, dict):
            raise PasskeyStoreError("WebAuthn challenge context must be an object.")
        return value


@dataclass(frozen=True, slots=True)
class PasskeyCredential:
    credential_id: str
    public_key: bytes
    user_handle: str
    sign_count: int
    transports: tuple[str, ...]
    created_at: datetime
    last_used_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PolicyApprovalReceipt:
    receipt_id: str
    revision_id: str
    policy_digest: str
    readiness_digest: str
    control_epoch: int
    credential_id: str
    challenge_id: str
    assertion_digest: str
    recorded_at: datetime
    receipt_digest: str

    @classmethod
    def issue(
        cls,
        *,
        revision_id: str,
        policy_digest: str,
        readiness_digest: str,
        control_epoch: int,
        credential_id: str,
        challenge_id: str,
        assertion_digest: str,
        recorded_at: datetime,
    ) -> "PolicyApprovalReceipt":
        values = {
            "receipt_id": new_id(), "revision_id": revision_id,
            "policy_digest": policy_digest, "readiness_digest": readiness_digest,
            "control_epoch": control_epoch, "credential_id": credential_id,
            "challenge_id": challenge_id, "assertion_digest": assertion_digest,
            "recorded_at": iso_utc(recorded_at),
        }
        return cls(
            receipt_id=values["receipt_id"], revision_id=revision_id,
            policy_digest=policy_digest, readiness_digest=readiness_digest,
            control_epoch=control_epoch, credential_id=credential_id,
            challenge_id=challenge_id, assertion_digest=assertion_digest,
            recorded_at=recorded_at.astimezone(timezone.utc),
            receipt_digest=_receipt_digest(values),
        )


def _receipt_payload(receipt: PolicyApprovalReceipt) -> dict[str, Any]:
    return {
        "receipt_id": receipt.receipt_id,
        "revision_id": receipt.revision_id,
        "policy_digest": receipt.policy_digest,
        "readiness_digest": receipt.readiness_digest,
        "control_epoch": receipt.control_epoch,
        "credential_id": receipt.credential_id,
        "challenge_id": receipt.challenge_id,
        "assertion_digest": receipt.assertion_digest,
        "recorded_at": iso_utc(receipt.recorded_at),
    }


def _receipt_digest(values: dict[str, Any]) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PasskeyStore:
    """SQLite persistence with atomic single-use challenges and immutable receipts."""

    def __init__(self, store: OrchestrationStore) -> None:
        if not isinstance(store, OrchestrationStore):
            raise TypeError("OrchestrationStore is required for passkey persistence.")
        self._store = store
        self.verify_integrity()

    def create_challenge(
        self,
        *,
        purpose: str,
        context: dict[str, Any],
        expires_at: datetime,
    ) -> WebAuthnChallenge:
        if not isinstance(purpose, str) or purpose not in _PURPOSES:
            raise PasskeyStoreError("Unknown WebAuthn ceremony purpose.")
        if not isinstance(context, dict) or any(not isinstance(key, str) for key in context):
            raise PasskeyStoreError("WebAuthn challenge context must be a string-keyed object.")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise PasskeyStoreError("Challenge expiration must be timezone-aware.")
        created_at = utc_now()
        expires = expires_at.astimezone(timezone.utc)
        if expires <= created_at or expires > created_at + _MAX_CHALLENGE_AGE:
            raise PasskeyStoreError("WebAuthn challenge must expire within two minutes.")
        try:
            context_json = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                      allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise PasskeyStoreError("WebAuthn challenge context is not canonical JSON.") from exc
        challenge = WebAuthnChallenge(
            challenge_id=new_id(), challenge=secrets.token_urlsafe(32), purpose=purpose,
            context_json=context_json, created_at=created_at, expires_at=expires,
        )
        with self._store._connection() as db:
            db.execute(
                "DELETE FROM webauthn_challenges WHERE consumed_at IS NULL AND expires_at<=?",
                (iso_utc(created_at),),
            )
            if purpose == "register-passkey":
                if db.execute("SELECT 1 FROM passkey_credentials LIMIT 1").fetchone() is not None:
                    raise PasskeyStoreError("A passkey is already enrolled; remote reset is not supported.")
                if db.execute(
                        "SELECT 1 FROM webauthn_challenges WHERE purpose='register-passkey' "
                        "AND consumed_at IS NULL AND expires_at>? LIMIT 1",
                        (iso_utc(created_at),)).fetchone() is not None:
                    raise PasskeyStoreError("A passkey registration ceremony is already pending.")
            db.execute(
                "INSERT INTO webauthn_challenges VALUES(?,?,?,?,?,?,NULL)",
                (challenge.challenge_id, challenge.challenge, challenge.purpose, challenge.context_json,
                 iso_utc(challenge.created_at), iso_utc(challenge.expires_at)),
            )
        return challenge

    def consume_challenge(
        self,
        *,
        challenge_id: str,
        purpose: str,
        now: datetime,
    ) -> WebAuthnChallenge:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise PasskeyStoreError("Challenge consumption time must be timezone-aware.")
        instant = now.astimezone(timezone.utc)
        with self._store._connection() as db:
            row = db.execute(
                "SELECT challenge_id,challenge,purpose,context_json,created_at,expires_at,consumed_at "
                "FROM webauthn_challenges WHERE challenge_id=?", (challenge_id,),
            ).fetchone()
            if row is None or row[2] != purpose:
                raise PasskeyStoreError("WebAuthn challenge is missing or belongs to another ceremony.")
            if (row[6] is not None or parse_utc(row[4]) > instant
                    or parse_utc(row[5]) <= instant):
                raise PasskeyStoreError("WebAuthn challenge is expired or already consumed.")
            updated = db.execute(
                "UPDATE webauthn_challenges SET consumed_at=? WHERE challenge_id=? AND purpose=? "
                "AND consumed_at IS NULL AND expires_at>?",
                (iso_utc(instant), challenge_id, purpose, iso_utc(instant)),
            )
            if updated.rowcount != 1:
                raise PasskeyStoreError("WebAuthn challenge was concurrently consumed.")
        return WebAuthnChallenge(row[0], row[1], row[2], row[3], parse_utc(row[4]), parse_utc(row[5]))

    def complete_registration(self, *, challenge_id: str, now: datetime,
                              credential: PasskeyCredential) -> None:
        """Consume the first-enrollment challenge and save its credential atomically."""
        instant = to_utc(now)
        self._validate_credential(credential)
        transports_json = json.dumps(credential.transports, separators=(",", ":"))
        with self._store._connection() as db:
            row = db.execute(
                "SELECT purpose,context_json,created_at,expires_at,consumed_at FROM webauthn_challenges "
                "WHERE challenge_id=?", (challenge_id,),
            ).fetchone()
            if (row is None or row[0] != "register-passkey" or row[4] is not None
                    or parse_utc(row[2]) > instant or parse_utc(row[3]) <= instant):
                raise PasskeyStoreError("Registration challenge is invalid, expired, or consumed.")
            try:
                context = json.loads(row[1])
            except (TypeError, json.JSONDecodeError):
                raise PasskeyStoreError("Registration challenge context is malformed.") from None
            if (not isinstance(context, dict) or context != {"user_handle": credential.user_handle}):
                raise PasskeyStoreError("Registration credential does not match its challenge owner.")
            if db.execute("SELECT 1 FROM passkey_credentials LIMIT 1").fetchone() is not None:
                raise PasskeyStoreError("A passkey is already enrolled; remote reset is not supported.")
            updated = db.execute(
                "UPDATE webauthn_challenges SET consumed_at=? WHERE challenge_id=? "
                "AND consumed_at IS NULL AND expires_at>?",
                (iso_utc(instant), challenge_id, iso_utc(instant)),
            )
            if updated.rowcount != 1:
                raise PasskeyStoreError("Registration challenge was concurrently consumed.")
            db.execute(
                "INSERT INTO passkey_credentials VALUES(?,?,?,?,?,?,?)",
                (credential.credential_id, sqlite3.Binary(credential.public_key), credential.user_handle,
                 credential.sign_count, transports_json, iso_utc(credential.created_at),
                 iso_utc(credential.last_used_at) if credential.last_used_at else None),
            )

    def complete_approval(self, *, receipt: PolicyApprovalReceipt, sign_count: int,
                          now: datetime) -> None:
        """Atomically advance the authenticator counter, consume challenge, and append receipt."""
        instant = to_utc(now)
        self._validate_receipt_values(receipt)
        if _receipt_digest(_receipt_payload(receipt)) != receipt.receipt_digest:
            raise PasskeyStoreError("Risk policy approval receipt digest is invalid.")
        if type(sign_count) is not int or sign_count < 0:
            raise PasskeyStoreError("Passkey assertion counter is invalid.")
        with self._store._connection() as db:
            challenge = db.execute(
                "SELECT purpose,context_json,created_at,expires_at,consumed_at "
                "FROM webauthn_challenges WHERE challenge_id=?", (receipt.challenge_id,),
            ).fetchone()
            if (challenge is None or challenge[0] != "approve-risk-policy" or challenge[4] is not None
                    or parse_utc(challenge[2]) > instant or parse_utc(challenge[3]) <= instant):
                raise PasskeyStoreError("Approval challenge is invalid, expired, or already consumed.")
            try:
                context = json.loads(challenge[1])
            except (TypeError, json.JSONDecodeError):
                raise PasskeyStoreError("Approval challenge context is malformed.") from None
            if (not isinstance(context, dict) or set(context) != {
                    "revision_id", "policy_digest", "readiness_digest", "control_epoch"
                } or context.get("revision_id") != receipt.revision_id
                    or context.get("policy_digest") != receipt.policy_digest
                    or context.get("readiness_digest") != receipt.readiness_digest
                    or type(context.get("control_epoch")) is not int
                    or context.get("control_epoch") != receipt.control_epoch):
                raise PasskeyStoreError("Approval receipt does not match its challenge context.")
            control = db.execute(
                "SELECT halted,control_epoch FROM orchestration_control WHERE singleton=1"
            ).fetchone()
            if control is None or control[0] != 0 or control[1] != receipt.control_epoch:
                raise PasskeyStoreError("Control state changed or halted during approval.")
            if (to_utc(receipt.recorded_at) != instant
                    or parse_utc(challenge[2]) > instant):
                raise PasskeyStoreError("Approval receipt timestamp is inconsistent.")
            credential = db.execute(
                "SELECT sign_count,last_used_at,created_at FROM passkey_credentials WHERE credential_id=?",
                (receipt.credential_id,),
            ).fetchone()
            if credential is None:
                raise PasskeyStoreError("Approval receipt references an unknown passkey.")
            previous = credential[0]
            if (previous != 0 or sign_count != 0) and sign_count <= previous:
                raise PasskeyStoreError("Passkey signature counter did not increase.")
            if iso_utc(instant) < credential[2] or (credential[1] and iso_utc(instant) < credential[1]):
                raise PasskeyStoreError("Passkey assertion timestamp moved backwards.")
            changed = db.execute(
                "UPDATE webauthn_challenges SET consumed_at=? WHERE challenge_id=? "
                "AND consumed_at IS NULL AND expires_at>?",
                (iso_utc(instant), receipt.challenge_id, iso_utc(instant)),
            )
            if changed.rowcount != 1:
                raise PasskeyStoreError("Approval challenge was concurrently consumed.")
            # The receipt trigger requires a consumed challenge. Keep this and
            # every following write in the same transaction so any failure rolls
            # the challenge consumption back as well.
            self._validate_receipt_links(db, receipt)
            db.execute(
                "UPDATE passkey_credentials SET sign_count=?,last_used_at=? WHERE credential_id=?",
                (sign_count, iso_utc(instant), receipt.credential_id),
            )
            db.execute(
                "INSERT INTO risk_policy_approval_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (receipt.receipt_id, receipt.revision_id, receipt.policy_digest,
                 receipt.readiness_digest, receipt.control_epoch, receipt.credential_id,
                 receipt.challenge_id, receipt.assertion_digest, iso_utc(receipt.recorded_at),
                 receipt.receipt_digest),
            )

    def save_credential(self, credential: PasskeyCredential) -> None:
        if not isinstance(credential, PasskeyCredential):
            raise TypeError("PasskeyCredential is required.")
        self._validate_credential(credential)
        transports_json = json.dumps(credential.transports, separators=(",", ":"))
        with self._store._connection() as db:
            db.execute(
                "INSERT INTO passkey_credentials VALUES(?,?,?,?,?,?,?)",
                (credential.credential_id, sqlite3.Binary(credential.public_key), credential.user_handle,
                 credential.sign_count, transports_json, iso_utc(credential.created_at),
                 iso_utc(credential.last_used_at) if credential.last_used_at else None),
            )

    @staticmethod
    def _validate_credential(credential: PasskeyCredential) -> None:
        if (not isinstance(credential, PasskeyCredential)
                or not isinstance(credential.credential_id, str)
                or _CREDENTIAL_ID.fullmatch(credential.credential_id) is None
                or not isinstance(credential.public_key, bytes) or not credential.public_key
                or len(credential.public_key) > 16384
                or not isinstance(credential.user_handle, str) or not credential.user_handle
                or len(credential.user_handle) > 256
                or type(credential.sign_count) is not int or credential.sign_count < 0
                or not isinstance(credential.transports, tuple)
                or any(not isinstance(item, str) or len(item) > 40 for item in credential.transports)
                or len(set(credential.transports)) != len(credential.transports)
                or not isinstance(credential.created_at, datetime)
                or credential.created_at.tzinfo is None or credential.created_at.utcoffset() is None
                or (credential.last_used_at is not None
                    and (not isinstance(credential.last_used_at, datetime)
                         or credential.last_used_at.tzinfo is None
                         or credential.last_used_at.utcoffset() is None))):
            raise PasskeyStoreError("Passkey credential fields are invalid.")

    def credential(self, credential_id: str) -> PasskeyCredential | None:
        with self._store._reader() as db:
            row = db.execute(
                "SELECT credential_id,public_key,user_handle,sign_count,transports_json,created_at,last_used_at "
                "FROM passkey_credentials WHERE credential_id=?", (credential_id,),
            ).fetchone()
        return self._decode_credential(row) if row is not None else None

    def credentials(self) -> tuple[PasskeyCredential, ...]:
        with self._store._reader() as db:
            rows = db.execute(
                "SELECT credential_id,public_key,user_handle,sign_count,transports_json,created_at,last_used_at "
                "FROM passkey_credentials ORDER BY created_at,credential_id"
            ).fetchall()
        return tuple(self._decode_credential(row) for row in rows)

    def challenge_by_value(self, challenge: str, *, purpose: str) -> WebAuthnChallenge | None:
        if not isinstance(challenge, str) or not challenge or purpose not in _PURPOSES:
            raise PasskeyStoreError("WebAuthn challenge lookup is invalid.")
        with self._store._reader() as db:
            row = db.execute(
                "SELECT challenge_id,challenge,purpose,context_json,created_at,expires_at "
                "FROM webauthn_challenges WHERE challenge=? AND purpose=?",
                (challenge, purpose),
            ).fetchone()
        if row is None:
            return None
        return WebAuthnChallenge(row[0], row[1], row[2], row[3], parse_utc(row[4]), parse_utc(row[5]))


    @staticmethod
    def _decode_credential(row: sqlite3.Row) -> PasskeyCredential:
        try:
            transports = json.loads(row[4])
            if not isinstance(transports, list) or any(not isinstance(item, str) for item in transports):
                raise ValueError("invalid credential transports")
            return PasskeyCredential(
                credential_id=row[0], public_key=bytes(row[1]), user_handle=row[2], sign_count=row[3],
                transports=tuple(transports), created_at=parse_utc(row[5]),
                last_used_at=parse_utc(row[6]) if row[6] else None,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PasskeyStoreError("Stored passkey credential is malformed.") from exc

    def update_credential_use(self, credential_id: str, *, sign_count: int, used_at: datetime) -> None:
        if (type(sign_count) is not int or sign_count < 0 or not isinstance(used_at, datetime)
                or used_at.tzinfo is None or used_at.utcoffset() is None):
            raise PasskeyStoreError("Passkey assertion counter or timestamp is invalid.")
        with self._store._connection() as db:
            row = db.execute(
                "SELECT sign_count,last_used_at,created_at FROM passkey_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
            if row is None:
                raise PasskeyStoreError("Passkey assertion references an unknown credential.")
            previous = row[0]
            if iso_utc(used_at) < row[2]:
                raise PasskeyStoreError("Passkey assertion predates credential registration.")
            if (previous != 0 or sign_count != 0) and sign_count <= previous:
                raise PasskeyStoreError("Passkey signature counter did not increase.")
            if row[1] is not None and iso_utc(used_at) < row[1]:
                raise PasskeyStoreError("Passkey assertion timestamp moved backwards.")
            db.execute(
                "UPDATE passkey_credentials SET sign_count=?,last_used_at=? WHERE credential_id=?",
                (sign_count, iso_utc(used_at), credential_id),
            )

    def append_approval(self, receipt: PolicyApprovalReceipt) -> None:
        if not isinstance(receipt, PolicyApprovalReceipt):
            raise TypeError("PolicyApprovalReceipt is required.")
        self._validate_receipt_values(receipt)
        if _receipt_digest(_receipt_payload(receipt)) != receipt.receipt_digest:
            raise PasskeyStoreError("Risk policy approval receipt digest is invalid.")
        with self._store._connection() as db:
            self._validate_receipt_links(db, receipt)
            db.execute(
                "INSERT INTO risk_policy_approval_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (receipt.receipt_id, receipt.revision_id, receipt.policy_digest,
                 receipt.readiness_digest, receipt.control_epoch, receipt.credential_id,
                 receipt.challenge_id, receipt.assertion_digest, iso_utc(receipt.recorded_at),
                 receipt.receipt_digest),
            )

    def approval_receipt(self, revision_id: str) -> PolicyApprovalReceipt | None:
        with self._store._reader() as db:
            row = db.execute(
                "SELECT receipt_id,revision_id,policy_digest,readiness_digest,control_epoch,credential_id,"
                "challenge_id,assertion_digest,recorded_at,receipt_digest "
                "FROM risk_policy_approval_receipts WHERE revision_id=? ORDER BY recorded_at DESC,receipt_id DESC LIMIT 1",
                (revision_id,),
            ).fetchone()
        return self._decode_receipt(row) if row is not None else None

    def verify_integrity(self) -> None:
        with self._store._reader() as db:
            violations = db.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise PasskeyStoreError("Passkey runtime references are inconsistent.")
            credential_rows = db.execute(
                "SELECT credential_id,public_key,user_handle,sign_count,transports_json,created_at,last_used_at "
                "FROM passkey_credentials"
            ).fetchall()
            for credential_row in credential_rows:
                credential = self._decode_credential(credential_row)
                if (_CREDENTIAL_ID.fullmatch(credential.credential_id) is None
                        or not credential.public_key or not credential.user_handle
                        or type(credential.sign_count) is not int or credential.sign_count < 0
                        or len(set(credential.transports)) != len(credential.transports)):
                    raise PasskeyStoreError("Stored passkey credential failed integrity validation.")
            challenge_rows = db.execute(
                "SELECT challenge,purpose,context_json,created_at,expires_at,consumed_at FROM webauthn_challenges"
            ).fetchall()
            for challenge_row in challenge_rows:
                try:
                    context = json.loads(challenge_row[2])
                    created = parse_utc(challenge_row[3])
                    expires = parse_utc(challenge_row[4])
                    consumed = parse_utc(challenge_row[5]) if challenge_row[5] else None
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise PasskeyStoreError("Stored WebAuthn challenge is malformed.") from exc
                canonical_context = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                if (not isinstance(context, dict) or canonical_context != challenge_row[2]
                        or challenge_row[1] not in _PURPOSES or not isinstance(challenge_row[0], str)
                        or len(challenge_row[0]) < 32 or expires <= created
                        or expires - created > _MAX_CHALLENGE_AGE
                        or (consumed is not None and not created <= consumed < expires)):
                    raise PasskeyStoreError("Stored WebAuthn challenge failed integrity validation.")
            rows = db.execute(
                "SELECT receipt_id,revision_id,policy_digest,readiness_digest,control_epoch,credential_id,"
                "challenge_id,assertion_digest,recorded_at,receipt_digest "
                "FROM risk_policy_approval_receipts ORDER BY recorded_at,receipt_id"
            ).fetchall()
            for row in rows:
                receipt = self._decode_receipt(row)
                if _receipt_digest(_receipt_payload(receipt)) != receipt.receipt_digest:
                    raise PasskeyStoreError("Stored Risk approval receipt digest is invalid.")
                self._validate_receipt_links(db, receipt)

    @staticmethod
    def _decode_receipt(row: sqlite3.Row) -> PolicyApprovalReceipt:
        try:
            return PolicyApprovalReceipt(
                receipt_id=row[0], revision_id=row[1], policy_digest=row[2], readiness_digest=row[3],
                control_epoch=row[4], credential_id=row[5], challenge_id=row[6], assertion_digest=row[7],
                recorded_at=parse_utc(row[8]), receipt_digest=row[9],
            )
        except (TypeError, ValueError) as exc:
            raise PasskeyStoreError("Stored Risk approval receipt is malformed.") from exc

    @staticmethod
    def _validate_receipt_values(receipt: PolicyApprovalReceipt) -> None:
        if (not isinstance(receipt.receipt_id, str) or not receipt.receipt_id
                or not isinstance(receipt.revision_id, str) or not receipt.revision_id
                or not isinstance(receipt.credential_id, str) or not receipt.credential_id
                or not isinstance(receipt.challenge_id, str) or not receipt.challenge_id
                or type(receipt.control_epoch) is not int or receipt.control_epoch < 0
                or not isinstance(receipt.recorded_at, datetime)
                or receipt.recorded_at.tzinfo is None or receipt.recorded_at.utcoffset() is None
                or any(not isinstance(value, str) or _HEX_SHA256.fullmatch(value) is None
                       for value in (receipt.policy_digest, receipt.readiness_digest, receipt.assertion_digest,
                                     receipt.receipt_digest))):
            raise PasskeyStoreError("Risk approval receipt fields are invalid.")

    @staticmethod
    def _validate_receipt_links(db: sqlite3.Connection, receipt: PolicyApprovalReceipt) -> None:
        policy = db.execute(
            "SELECT policy_digest FROM risk_policy_revisions WHERE revision_id=?", (receipt.revision_id,),
        ).fetchone()
        if policy is None or policy[0] != receipt.policy_digest:
            raise PasskeyStoreError("Risk approval receipt does not match the immutable policy revision.")
        challenge = db.execute(
            "SELECT purpose,context_json,consumed_at,expires_at FROM webauthn_challenges WHERE challenge_id=?",
            (receipt.challenge_id,),
        ).fetchone()
        if challenge is None or challenge[0] != "approve-risk-policy" or challenge[2] is None:
            raise PasskeyStoreError("Risk approval receipt must reference a consumed approval challenge.")
        try:
            context = json.loads(challenge[1])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PasskeyStoreError("Risk approval challenge context is malformed.") from exc
        if (not isinstance(context, dict) or set(context) != {
                    "revision_id", "policy_digest", "readiness_digest", "control_epoch"
                } or type(context.get("control_epoch")) is not int
                or context.get("revision_id") != receipt.revision_id
                or context.get("policy_digest") != receipt.policy_digest
                or context.get("readiness_digest") != receipt.readiness_digest
                or context.get("control_epoch") != receipt.control_epoch):
            raise PasskeyStoreError("Risk approval receipt does not match its bound challenge context.")
        if (parse_utc(challenge[2]) > receipt.recorded_at.astimezone(timezone.utc)
                or parse_utc(challenge[2]) >= parse_utc(challenge[3])):
            raise PasskeyStoreError("Risk approval receipt predates challenge consumption.")
        if db.execute("SELECT 1 FROM passkey_credentials WHERE credential_id=?", (receipt.credential_id,)).fetchone() is None:
            raise PasskeyStoreError("Risk approval receipt references an unknown passkey credential.")
