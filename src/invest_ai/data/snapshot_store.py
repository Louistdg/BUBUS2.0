"""Immutable local registry for trusted, content-verified market snapshots.

The registry provides research snapshots and a stricter opt-in eligibility
check for a trading-risk input. It does not execute or authorize trades.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from invest_ai.data.archive import ArchiveError
from invest_ai.data.market import MarketSnapshot, build_market_snapshot
from invest_ai.data.models import (
    DataContractError, Instrument, MarketBar, Quote, as_utc, canonical_json_bytes,
)


class SnapshotStoreError(RuntimeError):
    """A snapshot is missing, corrupted, replaced, or ineligible."""


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    snapshot: MarketSnapshot
    snapshot_sha256: str
    normalized_reference: str
    normalized_sha256: str
    observation_type: str

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id


@dataclass(frozen=True, slots=True)
class SnapshotAssessment:
    snapshot_id: str
    eligible: bool
    purpose: str
    reasons: tuple[str, ...]


def _safe_relative(root: Path, reference: str) -> Path:
    if not isinstance(reference, str) or not reference.strip():
        raise SnapshotStoreError("archive reference is required")
    candidate = (root / Path(reference.replace("\\", "/"))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SnapshotStoreError("archive reference escapes project root") from exc
    return candidate


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return as_utc(value, "timestamp").isoformat().replace("+00:00", "Z")
    if isinstance(value, timedelta):
        return str(value.total_seconds())
    if hasattr(value, "as_tuple"):  # Decimal, kept string-canonical
        return format(value, "f")
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise SnapshotStoreError(f"unsupported snapshot serialization type: {type(value).__name__}")


def _canonical_snapshot(snapshot: MarketSnapshot) -> bytes:
    return json.dumps(_json_value({
        name: getattr(snapshot, name)
        for name in snapshot.__dataclass_fields__
    }), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _instrument_dict(instrument: Instrument) -> dict[str, object]:
    values: dict[str, object] = {}
    for name in instrument.__dataclass_fields__:
        value = getattr(instrument, name)
        values[name] = _json_value(value)
    return values


def _instrument_from_dict(data: dict[str, object]) -> Instrument:
    from decimal import Decimal
    values = dict(data)
    for name in ("valid_from", "valid_to"):
        if values.get(name) is not None:
            values[name] = as_utc(str(values[name]), name)
    for name in ("multiplier", "tick_size", "lot_size"):
        if values.get(name) is not None:
            values[name] = Decimal(str(values[name]))
    return Instrument(**values)  # type: ignore[arg-type]


def _observation_from_dict(data: dict[str, object]) -> MarketBar | Quote:
    from invest_ai.data.models import DataProvenance

    raw_p = data.get("provenance")
    if not isinstance(raw_p, dict):
        raise SnapshotStoreError("normalized observation provenance is missing")
    p = dict(raw_p)
    for name in ("observed_at", "published_at", "received_at", "available_at"):
        if p.get(name) is not None:
            p[name] = as_utc(str(p[name]), name)
    provenance = DataProvenance(**p)  # type: ignore[arg-type]
    if "interval" in data:
        values = dict(data)
        values.pop("provenance", None)
        for name in ("bar_start", "bar_end"):
            values[name] = as_utc(str(values[name]), name)
        return MarketBar(provenance=provenance, **values)  # type: ignore[arg-type]
    values = dict(data)
    values.pop("provenance", None)
    return Quote(provenance=provenance, **values)  # type: ignore[arg-type]


class MarketSnapshotStore:
    """Append-only snapshot registry backed by the existing DATA archives."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        if not self.project_root.is_dir():
            raise SnapshotStoreError("project root must already exist")
        self.root = self.project_root / "data" / "market_snapshots"
        self.records = self.root / "records"

    def register(self, snapshot: MarketSnapshot, observation: MarketBar | Quote,
                 instrument: Instrument, *, max_age: timedelta) -> SnapshotRecord:
        """Rebuild from canonical inputs, verify both archives, then append."""
        if max_age < timedelta(0):
            raise SnapshotStoreError("max_age cannot be negative")
        try:
            rebuilt = build_market_snapshot(observation, instrument,
                                            as_of=snapshot.as_of, max_age=max_age)
        except DataContractError as exc:
            raise SnapshotStoreError(f"snapshot cannot be rebuilt: {exc}") from exc
        if rebuilt != snapshot:
            raise SnapshotStoreError("snapshot content does not match its source observation")

        provenance = observation.provenance
        raw_path = _safe_relative(self.project_root, provenance.raw_reference)
        if not raw_path.is_file():
            raise SnapshotStoreError("referenced raw payload is unavailable")
        raw_digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", provenance.raw_sha256) or raw_digest != provenance.raw_sha256:
            raise SnapshotStoreError("referenced raw payload does not match its SHA-256")

        normalized_reference = (
            Path("data") / "normalized" / provenance.provider.lower()
            / provenance.dataset.lower() / f"{provenance.observation_id}.json"
        ).as_posix()
        normalized_path = _safe_relative(self.project_root, normalized_reference)
        if not normalized_path.is_file():
            raise SnapshotStoreError("referenced normalized observation is unavailable")
        normalized_bytes = normalized_path.read_bytes()
        normalized_digest = hashlib.sha256(normalized_bytes).hexdigest()
        if hashlib.sha256(canonical_json_bytes(observation)).hexdigest() != normalized_digest:
            raise SnapshotStoreError("normalized observation was replaced or differs from its source")

        snapshot_bytes = _canonical_snapshot(snapshot)
        snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
        payload = {
            "schema": "invest-ai.market-snapshot.v1",
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_sha256": snapshot_digest,
            "snapshot": json.loads(snapshot_bytes),
            "instrument": _instrument_dict(instrument),
            "normalized_reference": normalized_reference,
            "normalized_sha256": normalized_digest,
            "raw_reference": provenance.raw_reference,
            "raw_sha256": raw_digest,
            "max_age_seconds": str(max_age.total_seconds()),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
        target = self.records / f"{snapshot.snapshot_id}.json"
        self._append_immutable(target, encoded)
        return SnapshotRecord(snapshot, snapshot_digest, normalized_reference, normalized_digest,
                              type(observation).__name__)

    def get(self, snapshot_id: str) -> SnapshotRecord:
        if not isinstance(snapshot_id, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
            raise SnapshotStoreError("snapshot id must be a lowercase SHA-256 digest")
        target = self.records / f"{snapshot_id}.json"
        if not target.is_file():
            raise SnapshotStoreError("snapshot record is unavailable")
        try:
            outer = json.loads(target.read_text(encoding="utf-8"))
            snap = outer["snapshot"]
            if outer["schema"] != "invest-ai.market-snapshot.v1" or outer["snapshot_id"] != snapshot_id:
                raise SnapshotStoreError("snapshot record identity or schema mismatch")
            snapshot_bytes = json.dumps(snap, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False).encode("utf-8")
            digest = hashlib.sha256(snapshot_bytes).hexdigest()
            if digest != outer["snapshot_sha256"]:
                raise SnapshotStoreError("snapshot content digest mismatch")
            fields = dict(snap)
            for name in ("observed_at", "published_at", "received_at", "available_at", "as_of"):
                if fields.get(name) is not None:
                    fields[name] = as_utc(str(fields[name]), name)
            from decimal import Decimal
            for name in ("price", "multiplier", "tick_size", "lot_size"):
                if fields.get(name) is not None:
                    fields[name] = Decimal(str(fields[name]))
            snapshot = MarketSnapshot(**fields)
            if snapshot.snapshot_id != snapshot_id:
                raise SnapshotStoreError("snapshot id does not match canonical content")
            record = SnapshotRecord(snapshot, digest, outer["normalized_reference"],
                                    outer["normalized_sha256"], "")
            self._verify_record(record)
            observation = self._verify_reconstruction(outer, record)
            return SnapshotRecord(snapshot, digest, outer["normalized_reference"],
                                  outer["normalized_sha256"], type(observation).__name__)
        except SnapshotStoreError:
            raise
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SnapshotStoreError("snapshot record is unreadable or malformed") from exc

    def assess_for_research(self, snapshot_id: str, *, as_of: datetime | str) -> SnapshotAssessment:
        record = self.get(snapshot_id)
        cutoff = as_utc(as_of, "as_of")
        reasons: list[str] = []
        if (record.snapshot.observed_at > cutoff or record.snapshot.available_at > cutoff
                or (record.snapshot.published_at is not None and record.snapshot.published_at > cutoff)):
            reasons.append("FUTURE_OR_NOT_YET_AVAILABLE")
        return SnapshotAssessment(snapshot_id, not reasons, "RESEARCH", tuple(reasons))

    def assess_for_risk(self, snapshot_id: str, *, as_of: datetime | str,
                        max_age: timedelta) -> SnapshotAssessment:
        record = self.get(snapshot_id)
        snapshot = record.snapshot
        cutoff = as_utc(as_of, "as_of")
        reasons: list[str] = []
        if (snapshot.observed_at > cutoff or snapshot.available_at > cutoff
                or (snapshot.published_at is not None and snapshot.published_at > cutoff)):
            reasons.append("FUTURE_OR_NOT_YET_AVAILABLE")
        if snapshot.observed_at < cutoff - max_age:
            reasons.append("STALE")
        # This is deliberately only an admissibility assessment, not authorization.
        if record.observation_type != "Quote":
            reasons.append("OBSERVATION_NOT_QUOTE")
        if snapshot.dataset != "quote":
            reasons.append("NOT_A_QUOTE")
        if record.observation_type == "Quote" and snapshot.dataset != "quote":
            reasons.append("OBSERVATION_DATASET_MISMATCH")
        if snapshot.adjusted is True:
            reasons.append("ADJUSTED_PRICE")
        if snapshot.adjusted is None and snapshot.dataset == "market_bar":
            reasons.append("ADJUSTMENT_STATUS_UNKNOWN")
        return SnapshotAssessment(snapshot_id, not reasons, "RISK_PRICE_CANDIDATE", tuple(reasons))

    def _verify_record(self, record: SnapshotRecord) -> None:
        outer_path = self.records / f"{record.snapshot_id}.json"
        try:
            outer = json.loads(outer_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SnapshotStoreError("snapshot record is unavailable or unreadable") from exc
        raw_path = _safe_relative(self.project_root, outer["raw_reference"])
        if not raw_path.is_file() or hashlib.sha256(raw_path.read_bytes()).hexdigest() != outer["raw_sha256"]:
            raise SnapshotStoreError("raw payload is unavailable or has changed")
        if outer["raw_sha256"] != record.snapshot.raw_sha256:
            raise SnapshotStoreError("snapshot raw hash no longer matches record")
        normalized_path = _safe_relative(self.project_root, record.normalized_reference)
        if not normalized_path.is_file():
            raise SnapshotStoreError("normalized source is unavailable")
        normalized = normalized_path.read_bytes()
        if hashlib.sha256(normalized).hexdigest() != record.normalized_sha256:
            raise SnapshotStoreError("normalized source was replaced")
        if hashlib.sha256(normalized).hexdigest() != outer["normalized_sha256"]:
            raise SnapshotStoreError("normalized source digest does not match snapshot manifest")

    def _verify_reconstruction(self, outer: dict[str, object],
                               record: SnapshotRecord) -> MarketBar | Quote:
        normalized_path = _safe_relative(self.project_root, record.normalized_reference)
        try:
            normalized_data = json.loads(normalized_path.read_text(encoding="utf-8"))
            observation = _observation_from_dict(normalized_data)
            instrument_data = outer["instrument"]
            if not isinstance(instrument_data, dict):
                raise SnapshotStoreError("instrument metadata is missing from snapshot record")
            instrument = _instrument_from_dict(instrument_data)
            max_age = timedelta(seconds=float(outer["max_age_seconds"]))
            rebuilt = build_market_snapshot(observation, instrument,
                                           as_of=record.snapshot.as_of, max_age=max_age)
            if rebuilt != record.snapshot:
                raise SnapshotStoreError("snapshot no longer matches reconstructed source content")
            return observation
        except SnapshotStoreError:
            raise
        except (OSError, KeyError, TypeError, ValueError, DataContractError) as exc:
            raise SnapshotStoreError("snapshot source cannot be reconstructed") from exc

    @staticmethod
    def _append_immutable(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != data:
                raise SnapshotStoreError("immutable snapshot id collision")
            return
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent,
                                             prefix=".tmp-", delete=False) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                if target.read_bytes() != data:
                    raise SnapshotStoreError("immutable snapshot id collision")
                os.unlink(temporary)
            else:
                os.rename(temporary, target)
        except OSError as exc:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            raise ArchiveError("snapshot record could not be appended atomically") from exc
