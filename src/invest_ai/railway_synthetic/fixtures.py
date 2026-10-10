"""Hand-authored demo entities; this module has no Core or filesystem imports."""
from __future__ import annotations

import hashlib

from .contract import build_snapshot


def synthetic_payload() -> dict:
    stamp = "2026-10-09T12:00:00.000Z"
    agents = [
        {"agent_id": "SYN-AGENT-ALPHA", "display_name": "Demo Observer", "demo_role": "DEMO_OBSERVER", "synthetic_only": True},
        {"agent_id": "SYN-AGENT-BRAVO", "display_name": "Demo Coordinator", "demo_role": "DEMO_COORDINATOR", "synthetic_only": True},
    ]
    missions = [
        {"mission_id": "SYN-MISSION-GARDEN", "title": "Demonstration garden survey", "demo_status": "DEMO_REVIEW", "demo_priority": "DEMO_NORMAL", "updated_at_utc": stamp, "synthetic_only": True},
        {"mission_id": "SYN-MISSION-LANTERN", "title": "Demonstration lantern inventory", "demo_status": "DEMO_PLANNED", "demo_priority": "DEMO_LOW", "updated_at_utc": stamp, "synthetic_only": True},
    ]
    dependencies = [
        {"dependency_id": "SYN-DEP-GARDEN-LANTERN", "mission_id": "SYN-MISSION-GARDEN", "depends_on_mission_id": "SYN-MISSION-LANTERN", "demo_relation": "DEMO_REQUIRES", "synthetic_only": True},
    ]
    claims = [
        {"claim_id": "SYN-CLAIM-NORTH", "mission_id": "SYN-MISSION-GARDEN", "holder_agent_id": "SYN-AGENT-BRAVO", "demo_scope": "SYNTHETIC_RESOURCE:MAP_TABLE", "demo_mode": "DEMO_READ", "demo_state": "DEMO_ACTIVE", "observed_at_utc": stamp, "synthetic_only": True},
    ]
    events = [
        {"event_id": "SYN-EVENT-001", "event_sequence": 1, "demo_type": "DEMO_MISSION_CREATED", "actor_agent_id": "SYN-AGENT-ALPHA", "occurred_at_utc": stamp, "synthetic_payload": {"mission_id": "SYN-MISSION-GARDEN"}, "synthetic_only": True},
        {"event_id": "SYN-EVENT-002", "event_sequence": 2, "demo_type": "DEMO_DEPENDENCY_LINKED", "actor_agent_id": "SYN-AGENT-BRAVO", "occurred_at_utc": stamp, "synthetic_payload": {"dependency_id": "SYN-DEP-GARDEN-LANTERN"}, "synthetic_only": True},
    ]
    evidence = [
        {"evidence_id": "SYN-EVIDENCE-001", "related_event_id": "SYN-EVENT-001", "demo_kind": "DEMO_NOTE", "artifact_ref": "synthetic://fixture/garden-note", "artifact_sha256": hashlib.sha256(b"invest-ai-railway-synthetic-fixture-v1:garden-note").hexdigest(), "verification_state": "SIMULATED_NOT_AUTHENTICATED", "created_at_utc": stamp, "synthetic_only": True},
    ]
    return {
        "agents": agents,
        "missions": missions,
        "dependencies": dependencies,
        "claims": claims,
        "events": events,
        "evidence": evidence,
        "demo_summary": {"simulated_state": "SIMULATED_YELLOW"},
    }


def make_first_snapshot():
    return build_snapshot(sequence=1, previous_snapshot_hash=None, created_at_utc="2026-10-09T12:00:00.000Z", payload=synthetic_payload())
