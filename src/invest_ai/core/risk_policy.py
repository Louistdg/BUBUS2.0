"""Fail-closed durable registry for immutable RiskPolicy revisions.

This foundation records candidate policy history only. It intentionally has no
approval API until an authenticated approval authority exists.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from invest_ai.core.clock import iso_utc, utc_now
from invest_ai.core.ids import new_id
from invest_ai.risk import AssetType, RiskPolicy, Side

if TYPE_CHECKING:
    from invest_ai.core.orchestration import OrchestrationStore


class RiskPolicyRegistryError(ValueError):
    """A policy revision cannot be safely recorded or resolved."""


class RiskPolicyUnavailable(RiskPolicyRegistryError):
    """No approved, effective, digest-verified policy is available."""


@dataclass(frozen=True, slots=True)
class RiskPolicyRevision:
    revision_id: str
    policy_version: str
    policy_digest: str
    approval_status: str
    effective_at: datetime
    recorded_at: datetime
    reason: str


class RiskPolicyRegistry:
    """Append-only policy history; candidate registration never grants authority."""

    def __init__(self, store: OrchestrationStore) -> None:
        from invest_ai.core.orchestration import OrchestrationStore

        if not isinstance(store, OrchestrationStore):
            raise TypeError("OrchestrationStore is required for the Risk policy registry.")
        self._store = store

    @staticmethod
    def _canonical_policy(policy: RiskPolicy) -> str:
        from invest_ai.core.orchestration import _dump

        if not isinstance(policy, RiskPolicy):
            raise TypeError("Canonical RiskPolicy required.")
        return _dump(policy)

    @staticmethod
    def _decode_policy(raw: str) -> RiskPolicy:
        try:
            data = json.loads(raw)
            decimal_fields = {
                "max_order_notional", "max_position_notional", "max_concentration_ratio",
                "max_sector_concentration_ratio", "max_gross_exposure", "min_cash_balance",
                "max_loss_per_trade", "max_daily_loss", "max_drawdown_ratio", "max_leverage",
                "fee_rate", "slippage_buffer_rate",
            }
            for name in decimal_fields:
                data[name] = Decimal(data[name]["$decimal"])
            data["allowed_instruments"] = frozenset(data["allowed_instruments"])
            data["allowed_sides"] = frozenset(Side(item) for item in data["allowed_sides"])
            data["allowed_asset_types"] = frozenset(AssetType(item) for item in data["allowed_asset_types"])
            policy = RiskPolicy(**data)
            if RiskPolicyRegistry._canonical_policy(policy) != raw:
                raise ValueError("non-canonical policy bytes")
            return policy
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RiskPolicyUnavailable("Stored Risk policy is malformed or non-canonical.") from exc

    def register_candidate(
        self,
        policy: RiskPolicy,
        *,
        effective_at: datetime,
        reason: str,
        recorded_at: datetime | None = None,
    ) -> RiskPolicyRevision:
        """Persist a PENDING revision. Caller text cannot approve or activate it."""
        if not isinstance(policy, RiskPolicy):
            raise TypeError("Canonical RiskPolicy required.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A non-empty reason is required for policy history.")
        if not isinstance(effective_at, datetime) or effective_at.tzinfo is None or effective_at.utcoffset() is None:
            raise ValueError("effective_at must be timezone-aware.")
        created_at = recorded_at or utc_now()
        if not isinstance(created_at, datetime) or created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware.")
        canonical = self._canonical_policy(policy)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        revision_id = new_id()
        effective_text, recorded_text = iso_utc(effective_at), iso_utc(created_at)
        with self._store._connection() as db:
            db.execute(
                "INSERT INTO risk_policy_revisions VALUES(?,?,?,?,?,?,?,?)",
                (revision_id, policy.version, canonical, digest, "PENDING", effective_text,
                 recorded_text, reason.strip()[:500]),
            )
        return RiskPolicyRevision(revision_id, policy.version, digest, "PENDING",
                                  effective_at.astimezone(timezone.utc), created_at.astimezone(timezone.utc),
                                  reason.strip()[:500])

    def history(self) -> tuple[RiskPolicyRevision, ...]:
        with self._store._reader() as db:
            rows = db.execute(
                "SELECT revision_id,policy_version,policy_digest,approval_status,effective_at,recorded_at,reason "
                "FROM risk_policy_revisions ORDER BY recorded_at,revision_id"
            ).fetchall()
        return tuple(RiskPolicyRevision(row[0], row[1], row[2], row[3],
                                        datetime.fromisoformat(row[4].replace("Z", "+00:00")),
                                        datetime.fromisoformat(row[5].replace("Z", "+00:00")), row[6])
                     for row in rows)

    def candidate(self, revision_id: str) -> tuple[RiskPolicyRevision, RiskPolicy] | None:
        """Return one digest-verified PENDING revision and its canonical policy."""
        if not isinstance(revision_id, str) or not revision_id.strip():
            raise ValueError("revision_id is required.")
        with self._store._reader() as db:
            row = db.execute(
                "SELECT revision_id,policy_version,policy_json,policy_digest,approval_status,effective_at,recorded_at,reason "
                "FROM risk_policy_revisions WHERE revision_id=?", (revision_id,),
            ).fetchone()
        if row is None:
            return None
        if row[4] != "PENDING" or hashlib.sha256(row[2].encode("utf-8")).hexdigest() != row[3]:
            raise RiskPolicyUnavailable("Risk policy candidate is not pending or failed its digest check.")
        policy = self._decode_policy(row[2])
        revision = RiskPolicyRevision(
            row[0], row[1], row[3], row[4], datetime.fromisoformat(row[5].replace("Z", "+00:00")),
            datetime.fromisoformat(row[6].replace("Z", "+00:00")), row[7],
        )
        return revision, policy

    def approval_receipt(self, revision_id: str):
        """Read an append-only local passkey receipt without activating execution."""
        from invest_ai.core.passkey_store import PasskeyStore

        return PasskeyStore(self._store).approval_receipt(revision_id)

    def active_policy(self, *, now: datetime | None = None) -> RiskPolicy:
        """Resolve only a digest-verified approved revision; absent authority vetoes."""
        instant = now or utc_now()
        if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("now must be timezone-aware.")
        with self._store._reader() as db:
            pointer = db.execute("SELECT active_revision_id FROM risk_policy_control WHERE singleton=1").fetchone()
            if pointer is None or not pointer[0]:
                raise RiskPolicyUnavailable("No approved active Risk policy is configured.")
            row = db.execute(
                "SELECT policy_json,policy_digest,approval_status,effective_at FROM risk_policy_revisions "
                "WHERE revision_id=?", (pointer[0],)
            ).fetchone()
        if row is None or row[2] != "APPROVED":
            raise RiskPolicyUnavailable("Active Risk policy revision is absent or not approved.")
        if datetime.fromisoformat(row[3].replace("Z", "+00:00")) > instant.astimezone(timezone.utc):
            raise RiskPolicyUnavailable("Active Risk policy is not yet effective.")
        if hashlib.sha256(row[0].encode("utf-8")).hexdigest() != row[1]:
            raise RiskPolicyUnavailable("Active Risk policy digest does not match its canonical content.")
        return self._decode_policy(row[0])
