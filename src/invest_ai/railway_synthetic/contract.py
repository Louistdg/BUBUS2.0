"""Closed v1 synthetic snapshot contract and canonical integrity hashing."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import hashlib
from datetime import datetime, timezone
from typing import Any


CONTRACT = "invest-ai-coordination-synthetic"
SCHEMA_VERSION = 1
SOURCE_ID = "SYNTH-SOURCE-RAILWAY-DEMO-01"
MAX_BYTES = 128 * 1024
COLLECTION_LIMITS = {
    "agents": 20,
    "missions": 100,
    "dependencies": 200,
    "claims": 100,
    "events": 1000,
    "evidence": 200,
}
_HEX = re.compile(r"^[0-9a-f]{64}$")
_ID_PATTERNS = {
    "agent_id": re.compile(r"^SYN-AGENT-[A-Z0-9-]{1,40}$"),
    "mission_id": re.compile(r"^SYN-MISSION-[A-Z0-9-]{1,40}$"),
    "dependency_id": re.compile(r"^SYN-DEP-[A-Z0-9-]{1,40}$"),
    "claim_id": re.compile(r"^SYN-CLAIM-[A-Z0-9-]{1,40}$"),
    "event_id": re.compile(r"^SYN-EVENT-[A-Z0-9-]{1,40}$"),
    "evidence_id": re.compile(r"^SYN-EVIDENCE-[A-Z0-9-]{1,40}$"),
}
_FIELDS = {
    "agents": {"agent_id", "display_name", "demo_role", "synthetic_only"},
    "missions": {"mission_id", "title", "demo_status", "demo_priority", "updated_at_utc", "synthetic_only"},
    "dependencies": {"dependency_id", "mission_id", "depends_on_mission_id", "demo_relation", "synthetic_only"},
    "claims": {"claim_id", "mission_id", "holder_agent_id", "demo_scope", "demo_mode", "demo_state", "observed_at_utc", "synthetic_only"},
    "events": {"event_id", "event_sequence", "demo_type", "actor_agent_id", "occurred_at_utc", "synthetic_payload", "synthetic_only"},
    "evidence": {"evidence_id", "related_event_id", "demo_kind", "artifact_ref", "artifact_sha256", "verification_state", "created_at_utc", "synthetic_only"},
}
_ENTITY_KEYS = {
    "agents": "agent_id", "missions": "mission_id", "dependencies": "dependency_id",
    "claims": "claim_id", "events": "event_id", "evidence": "evidence_id",
}
_ENVELOPE_FIELDS = {
    "contract", "schema_version", "dataset_mode", "source_instance_id", "sequence", "snapshot_id",
    "created_at_utc", "previous_snapshot_hash", "payload", "snapshot_sha256",
}
_PAYLOAD_FIELDS = set(COLLECTION_LIMITS) | {"demo_summary"}


class ContractError(ValueError):
    """A snapshot violates the closed synthetic contract."""


def _canonical(value: Any) -> bytes:
    """Stable UTF-8 JSON subset; the contract excludes floats and unsafe ints."""
    _validate_canonical_values(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _validate_canonical_values(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and unicodedata.normalize("NFC", value) != value:
            raise ContractError("strings must be Unicode NFC")
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > (2**53 - 1):
            raise ContractError("integer exceeds safe JSON range")
        return
    if isinstance(value, float):
        raise ContractError("floating point values are forbidden")
    if isinstance(value, list):
        for item in value:
            _validate_canonical_values(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or unicodedata.normalize("NFC", key) != key:
                raise ContractError("object keys must be NFC strings")
            _validate_canonical_values(item)
        return
    raise ContractError("unsupported JSON value")


def canonical_json(value: Any) -> bytes:
    return _canonical(value)


def parse_json_no_duplicates(raw: bytes) -> Any:
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ContractError("snapshot exceeds size limit")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ContractError("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ContractError("invalid numeric constant")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("invalid UTF-8 JSON") from exc


def _timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value):
        raise ContractError(f"{field} must be RFC3339 UTC with milliseconds")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ContractError(f"{field} is not a valid timestamp") from exc


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError(f"{label} has missing or additional fields")
    return value


def _id(value: Any, field: str) -> str:
    pattern = _ID_PATTERNS.get(field)
    if not isinstance(value, str) or pattern is None or not pattern.fullmatch(value):
        raise ContractError(f"{field} is outside synthetic ID namespace")
    return value


def _fixture_text(value: Any, field: str, allowed: set[str]) -> str:
    """Accept only reviewed, hand-authored fixture strings, never caller prose."""
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"{field} is not in the controlled synthetic fixture catalog")
    return value


_FIXTURE_TEXT = {
    "display_name": {"Demo Observer", "Demo Coordinator"},
    "title": {"Demonstration garden survey", "Demonstration lantern inventory"},
}
_FIXTURE_ARTIFACT_SHA = hashlib.sha256(b"invest-ai-railway-synthetic-fixture-v1:garden-note").hexdigest()


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    envelope = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    return hashlib.sha256(_canonical(envelope)).hexdigest()


def build_snapshot(*, sequence: int, previous_snapshot_hash: str | None, created_at_utc: str, payload: dict[str, Any], source_instance_id: str = SOURCE_ID) -> dict[str, Any]:
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ContractError("sequence must be a positive integer")
    snapshot = {
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "dataset_mode": "SYNTHETIC_ONLY",
        "source_instance_id": source_instance_id,
        "sequence": sequence,
        "snapshot_id": f"SYN-SNAPSHOT-{sequence:06d}",
        "created_at_utc": created_at_utc,
        "previous_snapshot_hash": previous_snapshot_hash,
        "payload": payload,
    }
    snapshot["snapshot_sha256"] = snapshot_digest(snapshot)
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot: Any, *, allow_hash_mismatch: bool = False) -> dict[str, Any]:
    root = _object(snapshot, _ENVELOPE_FIELDS, "snapshot")
    if root["contract"] != CONTRACT or root["schema_version"] != SCHEMA_VERSION:
        raise ContractError("unsupported contract or schema version")
    if root["dataset_mode"] != "SYNTHETIC_ONLY" or root["source_instance_id"] != SOURCE_ID:
        raise ContractError("snapshot source is not the configured synthetic source")
    if not isinstance(root["sequence"], int) or isinstance(root["sequence"], bool) or root["sequence"] < 1:
        raise ContractError("sequence must be a positive integer")
    if root["snapshot_id"] != f"SYN-SNAPSHOT-{root['sequence']:06d}":
        raise ContractError("snapshot ID does not match sequence")
    _timestamp(root["created_at_utc"], "created_at_utc")
    if root["previous_snapshot_hash"] is not None and (not isinstance(root["previous_snapshot_hash"], str) or not _HEX.fullmatch(root["previous_snapshot_hash"])):
        raise ContractError("previous_snapshot_hash is invalid")
    if not isinstance(root["snapshot_sha256"], str) or not _HEX.fullmatch(root["snapshot_sha256"]):
        raise ContractError("snapshot_sha256 is invalid")
    payload = _object(root["payload"], _PAYLOAD_FIELDS, "payload")

    ids: dict[str, set[str]] = {}
    for collection, limit in COLLECTION_LIMITS.items():
        items = payload[collection]
        if not isinstance(items, list) or len(items) > limit:
            raise ContractError(f"{collection} must be an array within its limit")
        expected_ids: list[str] = []
        found: set[str] = set()
        for item in items:
            item = _object(item, _FIELDS[collection], collection[:-1])
            key = _ENTITY_KEYS[collection]
            entity_id = _id(item[key], key)
            if entity_id in found:
                raise ContractError(f"duplicate {key}")
            found.add(entity_id)
            expected_ids.append(entity_id)
            if item["synthetic_only"] is not True:
                raise ContractError("every entity must set synthetic_only=true")
        if collection == "events":
            events = items
            seqs = [event["event_sequence"] for event in events]
            if any(not isinstance(seq, int) or isinstance(seq, bool) or seq < 1 for seq in seqs) or seqs != sorted(set(seqs)):
                raise ContractError("event_sequence must be unique and increasing")
            for event in events:
                if event["demo_type"] not in {"DEMO_MISSION_CREATED", "DEMO_STATUS_CHANGED", "DEMO_DEPENDENCY_LINKED", "DEMO_CLAIM_GRANTED", "DEMO_CLAIM_RELEASED", "DEMO_EVIDENCE_ATTACHED"}:
                    raise ContractError("unknown demo event type")
                if not isinstance(event["synthetic_payload"], dict) or set(event["synthetic_payload"]) - {"mission_id", "claim_id", "dependency_id", "from_status", "to_status"}:
                    raise ContractError("event payload has forbidden fields")
                for key, value in event["synthetic_payload"].items():
                    if key.endswith("_id"):
                        _id(value, key)
                    elif value not in {"DEMO_PLANNED", "DEMO_REVIEW", "DEMO_FINISHED"}:
                        raise ContractError("event payload status is not synthetic")
            if expected_ids != sorted(expected_ids, key=lambda _: _.encode("utf-8")):
                raise ContractError("events must be ordered by event_sequence")
        elif expected_ids != sorted(expected_ids, key=lambda _: _.encode("utf-8")):
            raise ContractError(f"{collection} must be sorted by ID")
        ids[collection] = found

    agents = {item["agent_id"] for item in payload["agents"]}
    missions = {item["mission_id"] for item in payload["missions"]}
    events = {item["event_id"] for item in payload["events"]}
    for item in payload["agents"]:
        if item["demo_role"] not in {"DEMO_OBSERVER", "DEMO_COORDINATOR", "DEMO_REVIEWER"}:
            raise ContractError("invalid demo agent")
        _fixture_text(item["display_name"], "display_name", _FIXTURE_TEXT["display_name"])
    for item in payload["missions"]:
        if item["demo_status"] not in {"DEMO_PLANNED", "DEMO_REVIEW", "DEMO_FINISHED"} or item["demo_priority"] not in {"DEMO_LOW", "DEMO_NORMAL", "DEMO_HIGH"}:
            raise ContractError("invalid demo mission enum")
        _fixture_text(item["title"], "title", _FIXTURE_TEXT["title"])
        _timestamp(item["updated_at_utc"], "updated_at_utc")
    for item in payload["dependencies"]:
        if item["mission_id"] not in missions or item["depends_on_mission_id"] not in missions or item["mission_id"] == item["depends_on_mission_id"] or item["demo_relation"] not in {"DEMO_REQUIRES", "DEMO_BLOCKS"}:
            raise ContractError("invalid synthetic dependency")
    for item in payload["claims"]:
        if item["mission_id"] not in missions or item["holder_agent_id"] not in agents:
            raise ContractError("claim references unknown synthetic entity")
        if not isinstance(item["demo_scope"], str) or not re.fullmatch(r"SYNTHETIC_RESOURCE:[A-Z0-9_-]{1,32}", item["demo_scope"]):
            raise ContractError("invalid synthetic scope")
        if item["demo_mode"] not in {"DEMO_READ", "DEMO_WRITE", "DEMO_EXCLUSIVE"} or item["demo_state"] not in {"DEMO_ACTIVE", "DEMO_RELEASED"}:
            raise ContractError("invalid demo claim enum")
        _timestamp(item["observed_at_utc"], "observed_at_utc")
    for item in payload["events"]:
        if item["actor_agent_id"] not in agents:
            raise ContractError("event references unknown synthetic agent")
        _timestamp(item["occurred_at_utc"], "occurred_at_utc")
    for item in payload["evidence"]:
        if item["related_event_id"] not in events or item["demo_kind"] not in {"DEMO_NOTE", "DEMO_CHECK"}:
            raise ContractError("invalid synthetic evidence reference")
        if not isinstance(item["artifact_ref"], str) or not item["artifact_ref"].startswith("synthetic://fixture/") or item["verification_state"] != "SIMULATED_NOT_AUTHENTICATED":
            raise ContractError("evidence must be a simulated synthetic reference")
        if not isinstance(item["artifact_sha256"], str) or not _HEX.fullmatch(item["artifact_sha256"]):
            raise ContractError("evidence artifact hash is invalid")
        expected_artifact = hashlib.sha256(b"invest-ai-railway-synthetic-fixture-v1:garden-note").hexdigest()
        if item["artifact_ref"] != "synthetic://fixture/garden-note" or item["artifact_sha256"] != expected_artifact:
            raise ContractError("evidence artifact must match the controlled fixture catalog")
        _timestamp(item["created_at_utc"], "created_at_utc")
    summary = _object(payload["demo_summary"], {"simulated_state"}, "demo_summary")
    if summary["simulated_state"] not in {"SIMULATED_GREEN", "SIMULATED_YELLOW", "SIMULATED_RED", "SIMULATED_UNKNOWN"}:
        raise ContractError("invalid simulated state")

    if not allow_hash_mismatch and root["snapshot_sha256"] != snapshot_digest(root):
        raise ContractError("snapshot hash mismatch")
    if len(_canonical(root)) > MAX_BYTES:
        raise ContractError("snapshot exceeds size limit")
    return root
