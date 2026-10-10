from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from invest_ai.coordination import (  # noqa: E402
    ClaimMode,
    ClaimScope,
    CoordinationError,
    CoordinationStore,
    DependencyType,
    EvidenceType,
    MissionStatus,
    ResourceMode,
    bootstrap_initial_state,
)


class CoordinationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "coordination.sqlite3"
        self.store = CoordinationStore(self.path)
        bootstrap_initial_state(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def declare(self, mission_id="M-1", agent="CODEX-A", status=MissionStatus.DECLARED):
        self.store.register_mission(mission_id, agent, "Test mission", status=status)

    def test_bootstrap_registers_six_canonical_agents(self):
        self.assertEqual({row["agent_id"] for row in self.store.list_agents()},
                         {"CODEX-A", "CODEX-B", "CODEX-C", "CODEX-D", "CODEX-E", "INVEST-AI-LEAD"})

    def test_create_mission(self):
        self.declare()
        self.assertEqual(self.store.mission("M-1")["status"], "DECLARED")

    def test_read_claims_can_overlap(self):
        self.declare("A", "CODEX-A")
        self.declare("D", "CODEX-D")
        self.store.create_claim("A", "CODEX-A", ClaimScope.PATH, "src/integration", ClaimMode.READ)
        self.store.create_claim("D", "CODEX-D", ClaimScope.PATH, "src/integration", ClaimMode.READ)
        self.assertEqual(self.store.verdict("A").color, "GREEN")

    def test_write_write_overlap_is_red(self):
        self.declare("A", "CODEX-A")
        self.declare("D", "CODEX-D")
        self.store.create_claim("A", "CODEX-A", ClaimScope.PATH, "src/integration", ClaimMode.WRITE)
        self.store.create_claim("D", "CODEX-D", ClaimScope.PATH, "src/integration/gateway.py", ClaimMode.WRITE)
        verdict = self.store.verdict("A")
        self.assertEqual(verdict.color, "RED")
        self.assertFalse(verdict.may_start)

    def test_read_write_overlap_is_compatible_per_policy(self):
        self.declare("A", "CODEX-A")
        self.declare("D", "CODEX-D")
        self.store.create_claim("A", "CODEX-A", ClaimScope.PATH, "src/integration", ClaimMode.WRITE)
        self.store.create_claim("D", "CODEX-D", ClaimScope.PATH, "src/integration", ClaimMode.READ)
        self.assertEqual(self.store.verdict("A").color, "GREEN")

    def test_exclusive_resource_conflict_is_red(self):
        self.declare("A", "CODEX-A")
        self.declare("D", "CODEX-D")
        self.store.create_claim("D", "CODEX-D", ClaimScope.RESOURCE, "GPU", ClaimMode.EXCLUSIVE)
        self.store.add_resource_requirement("A", "GPU", ResourceMode.EXCLUSIVE, actor="INVEST-AI-LEAD")
        self.store.set_resource_observation("GPU", 1, "active", "local operator", actor="INVEST-AI-LEAD")
        self.assertEqual(self.store.verdict("A").color, "RED")

    def test_active_mission_reserves_exclusive_resource(self):
        self.declare("A", "CODEX-A")
        self.declare("B", "CODEX-D")
        self.store.add_resource_requirement("A", "LEAN", ResourceMode.EXCLUSIVE, actor="INVEST-AI-LEAD")
        self.store.add_resource_requirement("B", "LEAN", ResourceMode.EXCLUSIVE, actor="INVEST-AI-LEAD")
        self.store.activate("A", actor="CODEX-A")
        self.assertEqual(self.store.verdict("B").color, "RED")

    def test_ram_threshold_unmet_waits_for_resources(self):
        self.store.set_resource_observation("RAM_FREE", 4, "GiB", "fixture", actor="INVEST-AI-LEAD")
        self.assertEqual(self.store.verdict("LLM-BENCHMARK-01").color, "YELLOW")
        self.assertTrue(any("threshold unmet" in r for r in self.store.verdict("LLM-BENCHMARK-01").reasons))
        self.assertEqual(self.store.mission("LLM-BENCHMARK-01")["status"], "WAITING_FOR_RESOURCES")
        with self.assertRaises(CoordinationError):
            self.store.activate("LLM-BENCHMARK-01", actor="CODEX-E")

    def test_forbidden_resource_without_value_fails_closed(self):
        self.store.set_resource_observation("OLLAMA_SOCLE", None, "active", "fixture", actor="INVEST-AI-LEAD")
        verdict = self.store.verdict("LLM-BENCHMARK-01")
        self.assertEqual(verdict.color, "YELLOW")
        self.assertFalse(verdict.may_start)
        self.assertTrue(any("cannot be evaluated" in reason for reason in verdict.reasons))

    def test_yellow_requires_explicit_lead_confirmation(self):
        self.declare("YELLOW-START", status=MissionStatus.YELLOW)
        with self.assertRaises(CoordinationError):
            self.store.activate("YELLOW-START", actor="CODEX-A", confirm_yellow=True)
        self.store.activate("YELLOW-START", actor="INVEST-AI-LEAD", confirm_yellow=True)
        self.assertEqual(self.store.mission("YELLOW-START")["status"], "ACTIVE")

    def test_required_dependency_becomes_satisfied_only_when_validated(self):
        self.declare("DEP", "CODEX-D")
        self.declare("CHILD", "CODEX-A")
        self.store.add_dependency("CHILD", "DEP", DependencyType.REQUIRES, actor="INVEST-AI-LEAD")
        self.assertEqual(self.store.verdict("CHILD").color, "RED")
        self.store.set_status("DEP", MissionStatus.VALIDATED, actor="INVEST-AI-LEAD")
        self.assertEqual(self.store.verdict("CHILD").color, "GREEN")

    def test_missing_dependency_fails_closed(self):
        self.declare("CHILD")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_dependency("CHILD", "UNKNOWN", DependencyType.REQUIRES, actor="INVEST-AI-LEAD")

    def test_blocks_dependency_propagates_red(self):
        self.declare("BLOCKER", "CODEX-D")
        self.declare("CHILD", "CODEX-A")
        self.store.add_dependency("CHILD", "BLOCKER", DependencyType.BLOCKS, actor="INVEST-AI-LEAD")
        self.assertEqual(self.store.verdict("CHILD").color, "RED")

    def test_green_yellow_red_verdicts(self):
        self.declare("GREEN")
        self.declare("YELLOW", status=MissionStatus.YELLOW)
        self.declare("RED", status=MissionStatus.RED)
        self.assertEqual(self.store.verdict("GREEN").color, "GREEN")
        self.assertEqual(self.store.verdict("YELLOW").color, "YELLOW")
        self.assertEqual(self.store.verdict("RED").color, "RED")

    def test_fin_reported_is_not_validation_and_cannot_start_with_unvalidated_dependency(self):
        self.declare("FIN", status=MissionStatus.FIN_REPORTED)
        self.declare("CHILD")
        self.store.add_dependency("CHILD", "FIN", DependencyType.REQUIRES, actor="INVEST-AI-LEAD")
        self.assertEqual(self.store.verdict("CHILD").color, "RED")
        with self.assertRaises(CoordinationError):
            self.store.activate("CHILD", actor="CODEX-A")

    def test_evidence_keeps_raw_and_agent_conclusion_as_distinct_types(self):
        self.declare("EVIDENCE")
        raw = self.store.add_evidence("EVIDENCE", EvidenceType.RAW_RESULT, "test", "stdout-ref", "CODEX-A", "Raw output")
        conclusion = self.store.add_evidence("EVIDENCE", EvidenceType.AGENT_CONCLUSION, "agent", "handoff-ref", "CODEX-A", "Interpretation")
        rows = self.store.evidence_for("EVIDENCE")
        self.assertEqual({row["evidence_id"] for row in rows}, {raw, conclusion})
        self.assertEqual({row["evidence_type"] for row in rows}, {"RAW_RESULT", "AGENT_CONCLUSION"})

    def test_only_lead_can_add_decisions_or_validate(self):
        self.declare("LEAD")
        with self.assertRaises(CoordinationError):
            self.store.add_decision("LEAD", "CODEX-A", "VALIDATE", "not allowed")
        with self.assertRaises(CoordinationError):
            self.store.set_status("LEAD", MissionStatus.VALIDATED, actor="CODEX-A")
        decision = self.store.add_decision("LEAD", "INVEST-AI-LEAD", "VALIDATE", "review passed")
        self.assertTrue(decision.startswith("decision-"))

    def test_only_lead_can_change_dependencies_and_resource_facts(self):
        self.declare("AUTH-A", "CODEX-A")
        self.declare("AUTH-D", "CODEX-D")
        with self.assertRaises(CoordinationError):
            self.store.add_dependency("AUTH-A", "AUTH-D", DependencyType.REQUIRES, actor="CODEX-A")
        with self.assertRaises(CoordinationError):
            self.store.set_resource_observation("RAM_FREE", 1, "GiB", "fixture", actor="CODEX-E")
        with self.assertRaises(CoordinationError):
            self.store.add_resource_requirement("AUTH-A", "GPU", ResourceMode.EXCLUSIVE, actor="CODEX-A")

    def test_events_evidence_and_decisions_are_append_only(self):
        self.declare("APPEND")
        evidence = self.store.add_evidence("APPEND", EvidenceType.RAW_RESULT, "test", "ref", "CODEX-A", "raw")
        decision = self.store.add_decision("APPEND", "INVEST-AI-LEAD", "REVIEW", "record review")
        db = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM events")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM evidence WHERE evidence_id=?", (evidence,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE decisions SET decision='REPLACE' WHERE decision_id=?", (decision,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM decisions WHERE decision_id=?", (decision,))
        finally:
            db.close()

    def test_claim_release_removes_conflict_and_records_history(self):
        self.declare("A", "CODEX-A")
        self.declare("B", "CODEX-D")
        claim_a = self.store.create_claim("A", "CODEX-A", ClaimScope.PATH, "src/shared", ClaimMode.WRITE)
        self.store.create_claim("B", "CODEX-D", ClaimScope.PATH, "src/shared", ClaimMode.WRITE)
        self.assertEqual(self.store.verdict("A").color, "RED")
        self.store.release_claim(claim_a, actor="CODEX-A", reason="work finished")
        self.assertEqual(self.store.verdict("A").color, "GREEN")
        self.assertTrue(any(row["event_type"] == "CLAIM_RELEASED" for row in self.store.events("A")))

    def test_paper_connect_mission_is_red_and_cannot_start(self):
        verdict = self.store.verdict("CONNECT-PAPER-EXECUTION")
        self.assertEqual(verdict.color, "RED")
        self.assertFalse(verdict.may_start)
        with self.assertRaises(CoordinationError):
            self.store.activate("CONNECT-PAPER-EXECUTION", actor="CODEX-A")

    def test_paper_connect_blocker_is_gateway_correction(self):
        verdict = self.store.verdict("CONNECT-PAPER-EXECUTION")
        self.assertTrue(any("INTEGRATION-GATEWAY-CORRECTION-01" in reason for reason in verdict.reasons))

    def test_persistence_after_reopen(self):
        self.declare("PERSIST")
        reopened = CoordinationStore(self.path)
        self.assertEqual(reopened.mission("PERSIST")["status"], "DECLARED")

    def test_verdict_is_deterministic(self):
        first = self.store.verdict("CONNECT-PAPER-EXECUTION")
        second = self.store.verdict("CONNECT-PAPER-EXECUTION")
        self.assertEqual(first, second)

    def test_bootstrap_is_idempotent(self):
        before = len(self.store.list_missions())
        events_before = len(self.store.events())
        bootstrap_initial_state(self.store)
        self.assertEqual(len(self.store.list_missions()), before)
        self.assertEqual(len(self.store.events()), events_before)

    def test_canonical_resource_catalog_is_available_without_observations(self):
        names = {item["resource_name"] for item in self.store.resources()}
        self.assertTrue({"RAM_FREE", "CPU", "GPU", "VRAM", "OLLAMA_INVEST", "OLLAMA_SOCLE",
                         "DOCKER", "LEAN", "NETWORK", "BROWSER", "REMOTE_PC"}.issubset(names))


if __name__ == "__main__":
    unittest.main()
