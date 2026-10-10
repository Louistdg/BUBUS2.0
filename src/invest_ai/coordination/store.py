"""SQLite-backed, append-only audit registry for INVEST-AI coordination."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from invest_ai.coordination.models import (
    ClaimMode,
    ClaimScope,
    CoordinationVerdict,
    DependencyType,
    EvidenceType,
    MissionStatus,
    ResourceMode,
)

LEAD = "INVEST-AI-LEAD"
RESOURCE_CATALOG = (
    "RAM_FREE", "CPU", "GPU", "VRAM", "OLLAMA_INVEST", "OLLAMA_SOCLE", "DOCKER",
    "LEAN", "NETWORK", "BROWSER", "REMOTE_PC",
)
AGENTS = {
    "CODEX-A": "CORE / ORCHESTRATION / POLICY / INTEGRATION",
    "CODEX-B": "DATA / PROVIDERS / OFFICIAL SOURCES",
    "CODEX-C": "BACKTEST / PAPER EXECUTION / TRADING ENGINE",
    "CODEX-D": "RISK / SECURITY / AUDIT / INDEPENDENT CONTROL",
    "CODEX-E": "LLM / RAG / KNOWLEDGE / LOCAL MODEL BENCHMARK",
    LEAD: "SUPERVISION / VALIDATION / ARBITRATION",
}

_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS agents(agent_id TEXT PRIMARY KEY, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS missions(
 mission_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(agent_id), title TEXT NOT NULL,
 status TEXT NOT NULL, priority TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 read_scopes_json TEXT NOT NULL, write_scopes_json TEXT NOT NULL,
 resource_requirements_json TEXT NOT NULL, handoff_path TEXT, notes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dependencies(
 mission_id TEXT NOT NULL REFERENCES missions(mission_id), depends_on TEXT NOT NULL REFERENCES missions(mission_id),
 dependency_type TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(mission_id,depends_on,dependency_type),
 CHECK(mission_id<>depends_on)
);
CREATE TABLE IF NOT EXISTS claims(
 claim_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 agent_id TEXT NOT NULL REFERENCES agents(agent_id), scope_type TEXT NOT NULL, scope TEXT NOT NULL,
 mode TEXT NOT NULL, created_at TEXT NOT NULL, released_at TEXT, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_observations(
 resource_name TEXT PRIMARY KEY, value REAL, unit TEXT NOT NULL, observed_at TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_catalog(resource_name TEXT PRIMARY KEY, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(
 evidence_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 evidence_type TEXT NOT NULL, source TEXT NOT NULL, path_or_reference TEXT NOT NULL,
 sha256 TEXT, created_at TEXT NOT NULL, agent_id TEXT NOT NULL REFERENCES agents(agent_id), description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions(
 decision_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(mission_id),
 actor TEXT NOT NULL REFERENCES agents(agent_id), decision TEXT NOT NULL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL, supersedes TEXT REFERENCES decisions(decision_id)
);
CREATE TABLE IF NOT EXISTS events(
 event_id TEXT PRIMARY KEY, mission_id TEXT REFERENCES missions(mission_id), event_type TEXT NOT NULL,
 actor TEXT NOT NULL, occurred_at TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_requirements(
 mission_id TEXT NOT NULL REFERENCES missions(mission_id), resource_name TEXT NOT NULL,
 mode TEXT NOT NULL, threshold_operator TEXT, threshold_value REAL, unit TEXT,
 PRIMARY KEY(mission_id,resource_name)
);
CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT,'evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT,'evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions BEGIN SELECT RAISE(ABORT,'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions BEGIN SELECT RAISE(ABORT,'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
"""


def _utc(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id(prefix: str, stable_key: str | None = None) -> str:
    digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24] if stable_key else uuid.uuid4().hex
    return f"{prefix}-{digest}"


class CoordinationError(ValueError):
    pass


class CoordinationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)
            db.execute("PRAGMA journal_mode=WAL")
            for resource_name in RESOURCE_CATALOG:
                db.execute("INSERT OR IGNORE INTO resource_catalog VALUES(?,?)",
                           (resource_name, "Canonical coordination resource"))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _event(self, db: sqlite3.Connection, event_type: str, actor: str, mission_id: str | None, payload: dict) -> None:
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                   (_id("evt"), mission_id, event_type, actor, _utc(), _json(payload)))

    def register_agents(self) -> None:
        with self._connect() as db:
            for agent_id, role in AGENTS.items():
                db.execute("INSERT OR IGNORE INTO agents VALUES(?,?)", (agent_id, role))

    def register_mission(
        self, mission_id: str, agent_id: str, title: str, *, status: MissionStatus = MissionStatus.DRAFT,
        priority: str = "NORMAL", read_scopes: tuple[str, ...] = (), write_scopes: tuple[str, ...] = (),
        resource_requirements: tuple[dict, ...] = (), handoff_path: str | None = None, notes: str = "",
        actor: str | None = None,
    ) -> None:
        if agent_id not in AGENTS or not mission_id.strip() or not title.strip():
            raise CoordinationError("mission_id, title and canonical agent are required")
        if actor is not None and actor not in {agent_id, LEAD}:
            raise CoordinationError("only the assigned agent or INVEST-AI-LEAD may register this mission")
        if not isinstance(status, MissionStatus):
            raise CoordinationError("status must be canonical")
        now = _utc()
        with self._connect() as db:
            existing = db.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
            if existing:
                if existing["agent_id"] != agent_id or existing["title"] != title:
                    raise CoordinationError("mission_id already exists with different identity")
                return
            db.execute("INSERT OR IGNORE INTO agents VALUES(?,?)", (agent_id, AGENTS[agent_id]))
            db.execute("""INSERT INTO missions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (mission_id, agent_id, title, status.value, priority, now, now,
                        _json(read_scopes), _json(write_scopes), _json(resource_requirements), handoff_path, notes))
            self._event(db, "MISSION_DECLARED", actor or agent_id, mission_id, {"status": status.value})

    def add_dependency(self, mission_id: str, depends_on: str, dependency_type: DependencyType,
                       *, actor: str) -> None:
        if actor != LEAD:
            raise CoordinationError("only INVEST-AI-LEAD may change mission dependencies")
        if not isinstance(dependency_type, DependencyType):
            raise CoordinationError("dependency_type must be canonical")
        with self._connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO dependencies VALUES(?,?,?,?)",
                             (mission_id, depends_on, dependency_type.value, _utc()))
            if cur.rowcount:
                self._event(db, "DEPENDENCY_ADDED", actor, mission_id,
                            {"depends_on": depends_on, "type": dependency_type.value})

    def set_status(self, mission_id: str, status: MissionStatus, *, actor: str, reason: str = "") -> None:
        if not isinstance(status, MissionStatus):
            raise CoordinationError("status must be canonical")
        with self._connect() as db:
            row = db.execute("SELECT agent_id,status FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
            if not row:
                raise CoordinationError("unknown mission")
            if status in {MissionStatus.VALIDATED, MissionStatus.CLOSED} and actor != LEAD:
                raise CoordinationError("only INVEST-AI-LEAD may validate or close a mission")
            if actor not in {row["agent_id"], LEAD}:
                raise CoordinationError("actor is not authorized for this mission")
            before = row["status"]
            db.execute("UPDATE missions SET status=?,updated_at=? WHERE mission_id=?", (status.value, _utc(), mission_id))
            self._event(db, "STATUS_CHANGED", actor, mission_id,
                        {"from": before, "to": status.value, "reason": reason})

    def create_claim(self, mission_id: str, agent_id: str, scope_type: ClaimScope, scope: str,
                     mode: ClaimMode, reason: str = "") -> str:
        if not isinstance(scope_type, ClaimScope) or not isinstance(mode, ClaimMode) or not scope.strip():
            raise CoordinationError("canonical claim type/mode and non-empty scope required")
        claim_id = _id("claim")
        with self._connect() as db:
            mission = db.execute("SELECT agent_id FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
            if not mission or mission["agent_id"] != agent_id:
                raise CoordinationError("claim owner must match mission agent")
            created = _utc()
            db.execute("INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?)",
                       (claim_id, mission_id, agent_id, scope_type.value, scope, mode.value, created, None, reason))
            self._event(db, "CLAIM_CREATED", agent_id, mission_id,
                        {"claim_id": claim_id, "scope_type": scope_type.value, "scope": scope, "mode": mode.value})
        return claim_id

    def release_claim(self, claim_id: str, *, actor: str, reason: str) -> None:
        with self._connect() as db:
            row = db.execute("SELECT mission_id,agent_id,released_at FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
            if not row:
                raise CoordinationError("unknown claim")
            if row["released_at"]:
                return
            if actor not in {row["agent_id"], LEAD}:
                raise CoordinationError("actor cannot release this claim")
            db.execute("UPDATE claims SET released_at=?,reason=? WHERE claim_id=?", (_utc(), reason, claim_id))
            self._event(db, "CLAIM_RELEASED", actor, row["mission_id"], {"claim_id": claim_id, "reason": reason})

    def set_resource_observation(self, resource_name: str, value: float | None, unit: str,
                                 source: str, *, actor: str, observed_at: datetime | None = None) -> None:
        if actor != LEAD:
            raise CoordinationError("only INVEST-AI-LEAD may record machine resource observations")
        if not resource_name.strip() or not unit.strip() or not source.strip():
            raise CoordinationError("resource, unit and source are required")
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise CoordinationError("resource observation must be numeric or absent")
        if value is not None and not math.isfinite(value):
            raise CoordinationError("resource observation must be finite")
        with self._connect() as db:
            db.execute("INSERT INTO resource_observations VALUES(?,?,?,?,?) ON CONFLICT(resource_name) DO UPDATE SET "
                       "value=excluded.value,unit=excluded.unit,observed_at=excluded.observed_at,source=excluded.source",
                       (resource_name, value, unit, _utc(observed_at), source))
            self._event(db, "RESOURCE_OBSERVED", actor, None,
                        {"resource": resource_name, "value": value, "unit": unit, "source": source})

    def add_resource_requirement(self, mission_id: str, resource_name: str, mode: ResourceMode,
                                 *, actor: str, operator: str | None = None, threshold: float | None = None,
                                 unit: str | None = None) -> None:
        if actor != LEAD:
            raise CoordinationError("only INVEST-AI-LEAD may change resource requirements")
        if not isinstance(mode, ResourceMode):
            raise CoordinationError("resource mode must be canonical")
        if mode is ResourceMode.THRESHOLD and (operator not in {">=", "<=", ">", "<"} or threshold is None or not unit):
            raise CoordinationError("threshold requires operator, value and unit")
        with self._connect() as db:
            desired = (mode.value, operator, threshold, unit)
            existing = db.execute("SELECT mode,threshold_operator,threshold_value,unit FROM resource_requirements "
                                  "WHERE mission_id=? AND resource_name=?", (mission_id, resource_name)).fetchone()
            if existing and tuple(existing) == desired:
                return
            db.execute("""INSERT INTO resource_requirements VALUES(?,?,?,?,?,?)
                ON CONFLICT(mission_id,resource_name) DO UPDATE SET mode=excluded.mode,
                threshold_operator=excluded.threshold_operator,threshold_value=excluded.threshold_value,unit=excluded.unit""",
                       (mission_id, resource_name, mode.value, operator, threshold, unit))
            self._event(db, "RESOURCE_REQUIREMENT_SET", LEAD, mission_id,
                        {"resource": resource_name, "mode": mode.value, "operator": operator,
                         "threshold": threshold, "unit": unit})

    def add_evidence(self, mission_id: str, evidence_type: EvidenceType, source: str,
                     path_or_reference: str, agent_id: str, description: str, sha256: str | None = None) -> str:
        if not isinstance(evidence_type, EvidenceType):
            raise CoordinationError("evidence type must be canonical")
        if not all(x.strip() for x in (source, path_or_reference, description)):
            raise CoordinationError("evidence source, reference and description are required")
        if evidence_type is EvidenceType.LEAD_DECISION and agent_id != LEAD:
            raise CoordinationError("only INVEST-AI-LEAD may add LEAD_DECISION evidence")
        if sha256 is not None and (len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256)):
            raise CoordinationError("sha256 must be lowercase hexadecimal")
        evidence_id = _id("evidence", f"{mission_id}|{evidence_type.value}|{path_or_reference}|{sha256 or ''}")
        with self._connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?,?,?)",
                       (evidence_id, mission_id, evidence_type.value, source, path_or_reference,
                        sha256, _utc(), agent_id, description))
            if cur.rowcount:
                self._event(db, "EVIDENCE_ADDED", agent_id, mission_id,
                            {"evidence_id": evidence_id, "type": evidence_type.value})
        return evidence_id

    def add_decision(self, mission_id: str, actor: str, decision: str, reason: str,
                     supersedes: str | None = None) -> str:
        if actor != LEAD:
            raise CoordinationError("only INVEST-AI-LEAD may create supervision decisions")
        if not decision.strip() or not reason.strip():
            raise CoordinationError("decision and reason are required")
        decision_id = _id("decision")
        with self._connect() as db:
            db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
                       (decision_id, mission_id, actor, decision, reason, _utc(), supersedes))
            self._event(db, "LEAD_DECISION", actor, mission_id,
                        {"decision_id": decision_id, "decision": decision, "reason": reason})
        return decision_id

    @staticmethod
    def _paths_overlap(left: str, right: str) -> bool:
        a, b = left.replace("\\", "/").rstrip("/").casefold(), right.replace("\\", "/").rstrip("/").casefold()
        return a == b or a.startswith(b + "/") or b.startswith(a + "/")

    def _active_claim_conflicts(self, db: sqlite3.Connection, mission_id: str) -> list[str]:
        own = db.execute("SELECT * FROM claims WHERE mission_id=? AND released_at IS NULL", (mission_id,)).fetchall()
        other = db.execute("SELECT * FROM claims WHERE mission_id<>? AND released_at IS NULL", (mission_id,)).fetchall()
        conflicts: list[str] = []
        for left in own:
            for right in other:
                if left["scope_type"] != right["scope_type"]:
                    continue
                if left["scope_type"] == ClaimScope.PATH.value:
                    overlap = self._paths_overlap(left["scope"], right["scope"])
                else:
                    overlap = left["scope"].casefold() == right["scope"].casefold()
                if not overlap:
                    continue
                mode_left, mode_right = left["mode"], right["mode"]
                if mode_left == ClaimMode.READ.value and mode_right == ClaimMode.READ.value:
                    continue
                if mode_left == ClaimMode.EXCLUSIVE.value or mode_right == ClaimMode.EXCLUSIVE.value:
                    conflicts.append(f"exclusive claim conflict: {left['scope']} ({right['mission_id']})")
                elif mode_left == ClaimMode.WRITE.value and mode_right == ClaimMode.WRITE.value:
                    conflicts.append(f"write/write claim conflict: {left['scope']} ({right['mission_id']})")
        return conflicts

    def _resource_issues(self, db: sqlite3.Connection, mission_id: str) -> tuple[list[str], list[str]]:
        warnings, conflicts = [], []
        requirements = db.execute("SELECT * FROM resource_requirements WHERE mission_id=?", (mission_id,)).fetchall()
        for req in requirements:
            mode = req["mode"]
            observation = db.execute("SELECT * FROM resource_observations WHERE resource_name=?",
                                     (req["resource_name"],)).fetchone()
            if not observation and mode != ResourceMode.EXCLUSIVE.value:
                warnings.append(f"resource not observed: {req['resource_name']}")
                continue
            if mode == ResourceMode.FORBIDDEN.value and observation["value"] is None:
                warnings.append(f"forbidden resource cannot be evaluated: {req['resource_name']}")
            elif mode == ResourceMode.FORBIDDEN.value and observation["value"] != 0:
                conflicts.append(f"forbidden resource active: {req['resource_name']}")
            elif mode == ResourceMode.THRESHOLD.value:
                if observation["unit"] != req["unit"] or observation["value"] is None:
                    warnings.append(f"resource threshold cannot be evaluated: {req['resource_name']}")
                    continue
                op, actual, threshold = req["threshold_operator"], observation["value"], req["threshold_value"]
                passed = {">=": actual >= threshold, "<=": actual <= threshold,
                          ">": actual > threshold, "<": actual < threshold}[op]
                if not passed:
                    warnings.append(f"resource threshold unmet: {req['resource_name']} {op} {threshold} {req['unit']}")
            elif mode == ResourceMode.EXCLUSIVE.value:
                holders = db.execute("""SELECT mission_id FROM claims WHERE scope_type='RESOURCE' AND scope=?
                    AND released_at IS NULL AND mission_id<>?""", (req["resource_name"], mission_id)).fetchall()
                active_requirements = db.execute("""SELECT r.mission_id FROM resource_requirements r
                    JOIN missions m ON m.mission_id=r.mission_id
                    WHERE r.resource_name=? AND r.mode='EXCLUSIVE' AND r.mission_id<>? AND m.status='ACTIVE'""",
                    (req["resource_name"], mission_id)).fetchall()
                if holders or active_requirements:
                    conflicts.append(f"exclusive resource in use: {req['resource_name']}")
        return warnings, conflicts

    def _verdict(self, db: sqlite3.Connection, mission_id: str) -> CoordinationVerdict:
        mission = db.execute("SELECT status FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
        if not mission:
            raise CoordinationError("unknown mission")
        reasons: list[str] = []
        for dependency in db.execute("SELECT * FROM dependencies WHERE mission_id=?", (mission_id,)):
            dep = db.execute("SELECT status FROM missions WHERE mission_id=?", (dependency["depends_on"],)).fetchone()
            dep_status = dep["status"] if dep else "MISSING"
            if dependency["dependency_type"] == DependencyType.REQUIRES.value and dep_status != MissionStatus.VALIDATED.value:
                reasons.append(f"requires {dependency['depends_on']} VALIDATED (currently {dep_status})")
            elif dependency["dependency_type"] == DependencyType.BLOCKS.value and dep_status not in {MissionStatus.VALIDATED.value, MissionStatus.CLOSED.value}:
                reasons.append(f"blocked by {dependency['depends_on']} ({dep_status})")
        claim_conflicts = self._active_claim_conflicts(db, mission_id)
        reasons.extend(claim_conflicts)
        resource_warnings, resource_conflicts = self._resource_issues(db, mission_id)
        reasons.extend(resource_conflicts)
        reasons.extend(resource_warnings)
        if mission["status"] in {MissionStatus.BLOCKED.value, MissionStatus.RED.value}:
            reasons.append(f"mission status is {mission['status']}")
        if (claim_conflicts or resource_conflicts
                or mission["status"] in {MissionStatus.BLOCKED.value, MissionStatus.RED.value}
                or any("requires " in r or "blocked by " in r for r in reasons)):
            color = "RED"
        elif resource_warnings or mission["status"] == MissionStatus.YELLOW.value:
            color = "YELLOW"
        else:
            color = "GREEN"
        return CoordinationVerdict(mission_id, color, tuple(reasons), color == "GREEN")

    def verdict(self, mission_id: str) -> CoordinationVerdict:
        with self._connect() as db:
            return self._verdict(db, mission_id)

    def activate(self, mission_id: str, *, actor: str, confirm_yellow: bool = False) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            mission = db.execute("SELECT agent_id,status FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
            if not mission:
                raise CoordinationError("unknown mission")
            if actor not in {mission["agent_id"], LEAD}:
                raise CoordinationError("actor is not authorized for this mission")
            if mission["status"] not in {MissionStatus.DECLARED.value, MissionStatus.YELLOW.value,
                                         MissionStatus.GREEN.value, MissionStatus.ACTIVE.value}:
                raise CoordinationError(f"mission status {mission['status']} cannot be started")
            if mission["status"] == MissionStatus.ACTIVE.value:
                return
            verdict = self._verdict(db, mission_id)
            if verdict.color == "RED":
                raise CoordinationError("RED verdict prevents mission start: " + "; ".join(verdict.reasons))
            unmet = any("resource threshold unmet" in reason or "resource not observed" in reason
                        or "threshold cannot be evaluated" in reason for reason in verdict.reasons)
            if unmet:
                raise CoordinationError("mission is waiting for resources: " + "; ".join(verdict.reasons))
            if verdict.color == "YELLOW" and not (confirm_yellow and actor == LEAD):
                raise CoordinationError("YELLOW requires explicit INVEST-AI-LEAD confirmation before start")
            requirements = db.execute("SELECT resource_name FROM resource_requirements WHERE mission_id=? AND mode='EXCLUSIVE'",
                                      (mission_id,)).fetchall()
            for req in requirements:
                claim_id = _id("claim")
                created = _utc()
                db.execute("INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?)",
                           (claim_id, mission_id, mission["agent_id"], ClaimScope.RESOURCE.value,
                            req["resource_name"], ClaimMode.EXCLUSIVE.value, created, None,
                            "Reserved automatically on mission activation"))
                self._event(db, "CLAIM_CREATED", actor, mission_id,
                            {"claim_id": claim_id, "scope_type": "RESOURCE", "scope": req["resource_name"],
                             "mode": "EXCLUSIVE", "automatic_reservation": True})
            db.execute("UPDATE missions SET status=?,updated_at=? WHERE mission_id=?",
                       (MissionStatus.ACTIVE.value, _utc(), mission_id))
            self._event(db, "STATUS_CHANGED", actor, mission_id,
                        {"from": mission["status"], "to": MissionStatus.ACTIVE.value,
                         "reason": f"coordination verdict={verdict.color}"})

    def list_agents(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM agents ORDER BY agent_id")]

    def list_missions(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM missions ORDER BY mission_id")]

    def mission(self, mission_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
            if not row:
                raise CoordinationError("unknown mission")
            data = dict(row)
            data["dependencies"] = [dict(item) for item in db.execute(
                "SELECT depends_on,dependency_type FROM dependencies WHERE mission_id=? ORDER BY depends_on", (mission_id,))]
            data["resource_requirements"] = [dict(item) for item in db.execute(
                "SELECT resource_name,mode,threshold_operator,threshold_value,unit FROM resource_requirements WHERE mission_id=? ORDER BY resource_name", (mission_id,))]
            data["verdict"] = self.verdict(mission_id).color
            return data

    def list_claims(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM claims ORDER BY created_at,claim_id")]

    def blockers(self) -> list[dict]:
        results = []
        for mission in self.list_missions():
            verdict = self.verdict(mission["mission_id"])
            if verdict.color == "RED":
                results.append({"mission_id": mission["mission_id"], "reasons": list(verdict.reasons)})
        return results

    def evidence_for(self, mission_id: str) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM evidence WHERE mission_id=? ORDER BY created_at,evidence_id", (mission_id,))]

    def resources(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("""SELECT c.resource_name,c.description,o.value,o.unit,o.observed_at,o.source
                FROM resource_catalog c LEFT JOIN resource_observations o USING(resource_name)
                ORDER BY c.resource_name""")]

    def events(self, mission_id: str | None = None) -> list[dict]:
        with self._connect() as db:
            if mission_id is None:
                rows = db.execute("SELECT * FROM events ORDER BY occurred_at,event_id")
            else:
                rows = db.execute("SELECT * FROM events WHERE mission_id=? ORDER BY occurred_at,event_id", (mission_id,))
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value.pop("payload_json"))
                result.append(value)
            return result

    def status_summary(self) -> dict:
        missions = self.list_missions()
        counts = {status.value: sum(item["status"] == status.value for item in missions) for status in MissionStatus}
        reds = sum(self.verdict(item["mission_id"]).color == "RED" for item in missions)
        return {"mission_count": len(missions), "status_counts": counts, "red_missions": reds}


def bootstrap_initial_state(store: CoordinationStore) -> None:
    """Idempotently seed the current cross-agent state from documented reports."""
    store.register_agents()
    seed = [
        ("INTEGRATION-GATEWAY-AUDIT-01", "CODEX-D", "Independent Gateway audit", MissionStatus.VALIDATED,
         "CRITICAL", "Audit deliverable validated; technical verdict is BLOCK. Source: CODEX-D audit handoff."),
        ("RISK-CANONICAL-INPUT-HASH-01", "CODEX-D", "Expose canonical Risk input hash", MissionStatus.DECLARED,
         "BLOCKER", "Declared prerequisite; no completion handoff found in bootstrap sources."),
        ("INTEGRATION-GATEWAY-CORRECTION-01", "CODEX-A", "Bind Gateway Policy input to exact Risk inputs", MissionStatus.BLOCKED,
         "CRITICAL", "Blocked pending RISK-CANONICAL-INPUT-HASH-01; Gateway remains BLOCKED_PENDING_REAUDIT."),
        ("PAPER-EXECUTION-FOUNDATION-01", "CODEX-C", "Pure PAPER execution foundation", MissionStatus.VALIDATED,
         "HIGH", "Mission bootstrap explicitly marks the foundation VALIDATED; no PAPER integration authorized."),
        ("LLM-BENCHMARK-01", "CODEX-E", "Local LLM benchmark", MissionStatus.WAITING_FOR_RESOURCES,
         "NORMAL", "Waiting for RAM_FREE >= 6 GiB; no current resource observation recorded."),
        ("OFFICIAL-ADAPTERS-PROTOTYPE-01", "CODEX-B", "Official read-only source adapters prototype", MissionStatus.FIN_REPORTED,
         "NORMAL", "Handoff recommends LEAD review; no explicit validation decision found, so FIN_REPORTED only."),
        ("POLICY-ENGINE-01", "CODEX-A", "Deterministic Policy Engine V0", MissionStatus.FIN_REPORTED,
         "HIGH", "CODEX-A reported completion; no LEAD validation decision found in available records."),
        ("CONNECT-PAPER-EXECUTION", "CODEX-A", "Connect approved decisions to PAPER execution", MissionStatus.DECLARED,
         "CRITICAL", "Coordination gate only; no execution or integration is authorized by this registry."),
    ]
    handoffs = {
        "INTEGRATION-GATEWAY-AUDIT-01": r"handoffs/CODEX-D/INTEGRATION-GATEWAY-AUDIT-01.md",
        "INTEGRATION-GATEWAY-CORRECTION-01": r"handoffs/CODEX-A/INTEGRATION-GATEWAY-CORRECTION-01.md",
        "PAPER-EXECUTION-FOUNDATION-01": r"handoffs/CODEX-C/PAPER-EXECUTION-FOUNDATION-01.md",
        "LLM-BENCHMARK-01": r"handoffs/CODEX-E/LLM-BENCHMARK-01.md",
        "OFFICIAL-ADAPTERS-PROTOTYPE-01": r"handoffs/CODEX-B/OFFICIAL-ADAPTERS-PROTOTYPE-01.md",
        "POLICY-ENGINE-01": r"handoffs/CODEX-A/POLICY-ENGINE-01.md",
    }
    for mission_id, agent, title, status, priority, notes in seed:
        scopes = {
            "INTEGRATION-GATEWAY-CORRECTION-01": (("src/invest_ai/integration/",), ("src/invest_ai/integration/", "tests/integration/")),
            "RISK-CANONICAL-INPUT-HASH-01": (("src/invest_ai/risk/",), ("src/invest_ai/risk/", "tests/risk/")),
        }.get(mission_id, ((), ()))
        store.register_mission(mission_id, agent, title, status=status, priority=priority,
                               read_scopes=scopes[0], write_scopes=scopes[1],
                               handoff_path=handoffs.get(mission_id), notes=notes)
        handoff = handoffs.get(mission_id)
        if handoff and Path(handoff).is_file():
            digest = hashlib.sha256(Path(handoff).read_bytes()).hexdigest()
            store.add_evidence(mission_id, EvidenceType.AGENT_CONCLUSION, "handoff artifact",
                               handoff, agent, "Handoff report registered as an agent conclusion; its hash identifies the report bytes.", digest)
    store.add_dependency("INTEGRATION-GATEWAY-CORRECTION-01", "RISK-CANONICAL-INPUT-HASH-01", DependencyType.REQUIRES, actor=LEAD)
    for dependency in ("INTEGRATION-GATEWAY-CORRECTION-01", "POLICY-ENGINE-01", "PAPER-EXECUTION-FOUNDATION-01"):
        store.add_dependency("CONNECT-PAPER-EXECUTION", dependency, DependencyType.REQUIRES, actor=LEAD)
    store.add_resource_requirement("LLM-BENCHMARK-01", "RAM_FREE", ResourceMode.THRESHOLD, actor=LEAD,
                                   operator=">=", threshold=6, unit="GiB")
    store.add_resource_requirement("LLM-BENCHMARK-01", "GPU", ResourceMode.EXCLUSIVE, actor=LEAD)
    store.add_resource_requirement("LLM-BENCHMARK-01", "OLLAMA_INVEST", ResourceMode.EXCLUSIVE, actor=LEAD)
    store.add_resource_requirement("LLM-BENCHMARK-01", "OLLAMA_SOCLE", ResourceMode.FORBIDDEN, actor=LEAD)
