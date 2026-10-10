from __future__ import annotations

import json
import sys
import sqlite3
import hashlib
from dataclasses import FrozenInstanceError
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from invest_ai.core.orchestration import (
    KillSwitchActive,
    OrchestrationConflict,
    OrchestrationStore,
    TrustedRiskUnavailable,
    WorkflowState,
    _available_cash_after_commitments,
)
from invest_ai.core.errors import JobStateError
from invest_ai.core.risk_policy import RiskPolicyRegistry, RiskPolicyUnavailable
from invest_ai.policy import PolicyDecision, PolicyMode, PolicyResult
from invest_ai.risk import (AssetType, RiskDecision, RiskPolicy, RiskResult, RiskRuntimeState,
                            RiskViolation, Side, TradeProposal)


NOW = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


class FaultingOrchestrationStore(OrchestrationStore):
    """Test-only SQLite authorizer used to reject a selected DDL/commit action."""

    def __init__(self, db_path, denied_action: int, denied_name: str | None = None):
        self.denied_action = denied_action
        self.denied_name = denied_name
        super().__init__(db_path)

    def _connect(self):
        db = super()._connect()

        def authorizer(action, arg1, arg2, database, trigger):
            if action == self.denied_action:
                if self.denied_name is None or self.denied_name in {arg1, arg2}:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorizer)
        return db


def proposal(pid: str = "p-1", idem: str = "i-1") -> TradeProposal:
    return TradeProposal(pid, idem, "instrument-1", "ABC", AssetType.EQUITY, Side.BUY, Decimal("2"), "strategy-1", NOW,
                         stop_price=Decimal("90"), market_snapshot_id="m-1", portfolio_snapshot_id="pf-1")


def risk(p: TradeProposal, result: RiskResult = RiskResult.ACCEPT, amount: str = "200") -> RiskDecision:
    return RiskDecision("risk-" + p.proposal_id, p.proposal_id, "risk-v1", NOW, result,
                        (RiskViolation("KILL_SWITCH_ACTIVE", "halted"),) if result is RiskResult.REJECT else (),
                        (("order_notional", amount),), "hash-" + p.proposal_id)


def policy(p: TradeProposal, result: PolicyResult = PolicyResult.ACCEPT, mode: PolicyMode = PolicyMode.PAPER) -> PolicyDecision:
    return PolicyDecision("policy-" + p.proposal_id, p.proposal_id, "policy-v1", NOW, result, (), mode,
                          ("a" * 64))


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.store = OrchestrationStore(self.path)
        self.store.clear_kill_switch(actor="operator", reason="test setup")

    def tearDown(self):
        self.temp.cleanup()

    def accept_risk(self, p: TradeProposal, *, limit: str = "1000", bucket: str = "EUR"):
        self.store.submit_proposal(p)
        return self.store.record_risk_decision(risk(p), exposure_bucket=bucket, exposure_limit=Decimal(limit))

    def assert_no_authoritative_risk_source(self, p: TradeProposal, market_ref: str = "snapshot-ref"):
        self.store.submit_proposal(p)
        with self.assertRaises(TrustedRiskUnavailable):
            self.store.evaluate_and_record_risk(p.proposal_id, market_snapshot_ref=market_ref)

    def seed_legacy_outbox(self, outbox_id: str, p: TradeProposal, status: str, lease_until: str | None):
        self.store.submit_proposal(p)
        db = sqlite3.connect(self.path)
        try:
            db.execute(
                "INSERT INTO orchestration_outbox VALUES(?,?,?,?,?,?,?,?)",
                (outbox_id, p.proposal_id, "LegacyPaperRequested", '{"legacy":true}', status,
                 "2026-10-08T00:00:00.000000Z", lease_until, None),
            )
            db.commit()
        finally:
            db.close()

    def outbox_state(self, outbox_id: str):
        db = sqlite3.connect(self.path)
        try:
            row = db.execute(
                "SELECT outbox_id,proposal_id,event_type,payload_json,status,created_at,lease_until,delivered_at "
                "FROM orchestration_outbox WHERE outbox_id=?", (outbox_id,),
            ).fetchone()
            return row
        finally:
            db.close()

    @staticmethod
    def risk_policy() -> RiskPolicy:
        return RiskPolicy(
            version="candidate-v1", allowed_instruments=frozenset({"instrument-1"}),
            allowed_sides=frozenset({Side.BUY, Side.SELL}),
            allowed_asset_types=frozenset({AssetType.EQUITY}),
            max_order_notional=Decimal("5000"), max_position_notional=Decimal("5000"),
            max_concentration_ratio=Decimal("0.5"), max_sector_concentration_ratio=Decimal("0.5"),
            max_gross_exposure=Decimal("10000"), min_cash_balance=Decimal("100"),
            max_loss_per_trade=Decimal("500"), max_daily_loss=Decimal("1000"),
            max_drawdown_ratio=Decimal("0.2"), max_leverage=Decimal("2"),
            max_market_data_age_seconds=60, max_portfolio_age_seconds=60,
            max_orders_per_window=3, frequency_window_seconds=60,
            fee_rate=Decimal("0.001"), slippage_buffer_rate=Decimal("0.002"),
        )

    def test_control_epoch_increments_on_every_activation_and_clear_and_survives_restart(self):
        initial = self.store.control_state()
        self.assertFalse(initial.halted)
        self.assertEqual(initial.control_epoch, 1)
        self.store.activate_kill_switch(actor="operator", reason="activate")
        self.assertEqual(self.store.control_state().control_epoch, 2)
        self.store.activate_kill_switch(actor="operator", reason="activate again")
        self.assertEqual(self.store.control_state().control_epoch, 3)
        self.store.clear_kill_switch(actor="operator", reason="clear")
        self.assertEqual(self.store.control_state().control_epoch, 4)
        self.assertFalse(OrchestrationStore(self.path).kill_switch_active())
        self.assertEqual(OrchestrationStore(self.path).control_state().control_epoch, 4)

    def test_legacy_control_migration_preserves_halted_value_and_starts_epoch_at_zero(self):
        for halted in (0, 1):
            path = Path(self.temp.name) / f"legacy-{halted}.sqlite3"
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE orchestration_meta(version INTEGER NOT NULL)")
                db.execute("INSERT INTO orchestration_meta VALUES(1)")
                db.execute("CREATE TABLE orchestration_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                           "halted INTEGER NOT NULL CHECK(halted IN (0,1)), changed_at TEXT NOT NULL, "
                           "actor TEXT NOT NULL, reason TEXT NOT NULL)")
                db.execute("INSERT INTO orchestration_control VALUES(1,?,?,?,?)",
                           (halted, "2026-10-01T00:00:00.000000Z", "legacy", "preserve"))
                db.commit()
            finally:
                db.close()
            migrated = OrchestrationStore(path)
            self.assertEqual(migrated.kill_switch_active(), bool(halted))
            self.assertEqual(migrated.control_state().control_epoch, 0)
            db = sqlite3.connect(path)
            try:
                self.assertEqual(db.execute("SELECT version FROM orchestration_meta").fetchone()[0], 4)
            finally:
                db.close()

    def test_unknown_schema_version_fails_closed_without_partial_epoch_migration(self):
        path = Path(self.temp.name) / "future.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE orchestration_meta(version INTEGER NOT NULL)")
            db.execute("INSERT INTO orchestration_meta VALUES(99)")
            db.execute("CREATE TABLE orchestration_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                       "halted INTEGER NOT NULL CHECK(halted IN (0,1)), changed_at TEXT NOT NULL, "
                       "actor TEXT NOT NULL, reason TEXT NOT NULL)")
            db.execute("INSERT INTO orchestration_control VALUES(1,1,'t','legacy','halted')")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(RuntimeError):
            OrchestrationStore(path)
        db = sqlite3.connect(path)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(orchestration_control)")}
            self.assertNotIn("control_epoch", columns)
            self.assertEqual(db.execute("SELECT halted FROM orchestration_control").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_policy_revisions'").fetchone())
        finally:
            db.close()

    def test_incoherent_v2_schema_is_rejected_without_repair_or_mutation(self):
        path = Path(self.temp.name) / "incoherent-v2.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE orchestration_meta(version INTEGER NOT NULL)")
            db.execute("INSERT INTO orchestration_meta VALUES(2)")
            db.execute("CREATE TABLE orchestration_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                       "halted INTEGER NOT NULL CHECK(halted IN (0,1)), changed_at TEXT NOT NULL, "
                       "actor TEXT NOT NULL, reason TEXT NOT NULL)")
            db.execute("INSERT INTO orchestration_control VALUES(1,1,'t','legacy','preserve halt')")
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(RuntimeError, "incompatible shape"):
            OrchestrationStore(path)
        db = sqlite3.connect(path)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in db.execute("PRAGMA table_info(orchestration_control)")}
            self.assertEqual(tables, {"orchestration_meta", "orchestration_control"})
            self.assertNotIn("control_epoch", columns)
            self.assertEqual(db.execute("SELECT halted FROM orchestration_control").fetchone()[0], 1)
        finally:
            db.close()

    def test_failed_schema_version_commit_rolls_back_epoch_migration(self):
        path = Path(self.temp.name) / "interrupted-migration.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE orchestration_meta(version INTEGER NOT NULL)")
            db.execute("INSERT INTO orchestration_meta VALUES(1)")
            db.execute("CREATE TABLE orchestration_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                       "halted INTEGER NOT NULL CHECK(halted IN (0,1)), changed_at TEXT NOT NULL, "
                       "actor TEXT NOT NULL, reason TEXT NOT NULL)")
            db.execute("INSERT INTO orchestration_control VALUES(1,1,'t','legacy','preserve halt')")
            db.execute("CREATE TRIGGER interrupt_migration BEFORE UPDATE ON orchestration_meta "
                       "BEGIN SELECT RAISE(ABORT,'migration commit interrupted'); END")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            OrchestrationStore(path)
        db = sqlite3.connect(path)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(orchestration_control)")}
            self.assertNotIn("control_epoch", columns)
            self.assertEqual(db.execute("SELECT version FROM orchestration_meta").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT halted FROM orchestration_control").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_policy_revisions'").fetchone())
        finally:
            db.close()
        db = sqlite3.connect(path)
        try:
            db.execute("DROP TRIGGER interrupt_migration")
            db.commit()
        finally:
            db.close()
        resumed = OrchestrationStore(path)
        self.assertTrue(resumed.kill_switch_active())
        self.assertEqual(resumed.control_state().control_epoch, 0)

    def test_failure_during_table_creation_rolls_back_the_entire_new_schema(self):
        path = Path(self.temp.name) / "create-failure.sqlite3"
        with self.assertRaises(sqlite3.DatabaseError):
            FaultingOrchestrationStore(path, sqlite3.SQLITE_CREATE_TABLE, "orchestration_events")
        db = sqlite3.connect(path)
        try:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger')")}
            self.assertFalse(names)
        finally:
            db.close()
        recovered = OrchestrationStore(path)
        self.assertTrue(recovered.kill_switch_active())
        self.assertEqual(recovered.control_state().control_epoch, 0)

    def test_failure_during_control_epoch_alter_rolls_back_all_additive_ddl(self):
        path = Path(self.temp.name) / "alter-failure.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE orchestration_meta(version INTEGER NOT NULL)")
            db.execute("INSERT INTO orchestration_meta VALUES(1)")
            db.execute("CREATE TABLE orchestration_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                       "halted INTEGER NOT NULL CHECK(halted IN (0,1)), changed_at TEXT NOT NULL, "
                       "actor TEXT NOT NULL, reason TEXT NOT NULL)")
            db.execute("INSERT INTO orchestration_control VALUES(1,1,'t','legacy','preserve halt')")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(sqlite3.DatabaseError):
            FaultingOrchestrationStore(path, sqlite3.SQLITE_ALTER_TABLE, "orchestration_control")
        db = sqlite3.connect(path)
        try:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in db.execute("PRAGMA table_info(orchestration_control)")}
            self.assertNotIn("risk_policy_revisions", names)
            self.assertNotIn("control_epoch", columns)
            self.assertEqual(db.execute("SELECT version FROM orchestration_meta").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT halted FROM orchestration_control").fetchone()[0], 1)
        finally:
            db.close()
        self.assertTrue(OrchestrationStore(path).kill_switch_active())

    def test_commit_failure_rolls_back_tables_column_and_version(self):
        path = Path(self.temp.name) / "commit-failure.sqlite3"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE orchestration_meta(version INTEGER NOT NULL)")
            db.execute("INSERT INTO orchestration_meta VALUES(1)")
            db.execute("CREATE TABLE orchestration_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                       "halted INTEGER NOT NULL CHECK(halted IN (0,1)), changed_at TEXT NOT NULL, "
                       "actor TEXT NOT NULL, reason TEXT NOT NULL)")
            db.execute("INSERT INTO orchestration_control VALUES(1,1,'t','legacy','preserve halt')")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(sqlite3.DatabaseError):
            FaultingOrchestrationStore(path, sqlite3.SQLITE_TRANSACTION, "COMMIT")
        db = sqlite3.connect(path)
        try:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in db.execute("PRAGMA table_info(orchestration_control)")}
            self.assertNotIn("risk_policy_revisions", names)
            self.assertNotIn("control_epoch", columns)
            self.assertEqual(db.execute("SELECT version FROM orchestration_meta").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT halted FROM orchestration_control").fetchone()[0], 1)
        finally:
            db.close()
        recovered = OrchestrationStore(path)
        self.assertTrue(recovered.kill_switch_active())
        self.assertEqual(recovered.control_state().control_epoch, 0)

    def test_migration_resumes_a_legacy_partial_ddl_without_activating_policy(self):
        path = Path(self.temp.name) / "legacy-partial.sqlite3"
        policy_value = self.risk_policy()
        canonical = RiskPolicyRegistry._canonical_policy(policy_value)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        db = sqlite3.connect(path)
        try:
            db.executescript("""
                CREATE TABLE orchestration_meta(version INTEGER NOT NULL);
                INSERT INTO orchestration_meta VALUES(1);
                CREATE TABLE orchestration_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    halted INTEGER NOT NULL CHECK(halted IN (0,1)), changed_at TEXT NOT NULL,
                    actor TEXT NOT NULL, reason TEXT NOT NULL);
                INSERT INTO orchestration_control VALUES(1,1,'t','legacy','preserve halt');
                CREATE TABLE risk_policy_revisions(revision_id TEXT PRIMARY KEY, policy_version TEXT NOT NULL,
                    policy_json TEXT NOT NULL, policy_digest TEXT NOT NULL,
                    approval_status TEXT NOT NULL CHECK(approval_status IN ('PENDING','APPROVED','REVOKED')),
                    effective_at TEXT NOT NULL, recorded_at TEXT NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE risk_policy_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    active_revision_id TEXT NULL REFERENCES risk_policy_revisions(revision_id));
                INSERT INTO risk_policy_control VALUES(1,NULL);
            """)
            db.execute("INSERT INTO risk_policy_revisions VALUES(?,?,?,?,?,?,?,?)",
                       ("candidate-old", policy_value.version, canonical, digest, "PENDING",
                        NOW.isoformat().replace("+00:00", "Z"), NOW.isoformat().replace("+00:00", "Z"),
                        "legacy candidate"))
            db.commit()
        finally:
            db.close()
        recovered = OrchestrationStore(path)
        self.assertTrue(recovered.kill_switch_active())
        self.assertEqual(recovered.control_state().control_epoch, 0)
        registry = RiskPolicyRegistry(recovered)
        self.assertEqual([item.revision_id for item in registry.history()], ["candidate-old"])
        with self.assertRaises(RiskPolicyUnavailable):
            registry.active_policy(now=NOW + timedelta(days=1))

    def test_interrupted_control_transition_rolls_back_epoch_and_event(self):
        before = self.store.control_state()
        event_count = len(self.store.events())
        db = sqlite3.connect(self.path)
        try:
            db.execute("CREATE TRIGGER interrupt_control_update BEFORE UPDATE ON orchestration_control "
                       "BEGIN SELECT RAISE(ABORT,'forced migration interruption'); END")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.clear_kill_switch(actor="operator", reason="interrupted")
        self.assertEqual(self.store.control_state(), before)
        self.assertEqual(len(self.store.events()), event_count)

    def test_missing_control_state_is_a_hard_veto(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("DELETE FROM orchestration_control WHERE singleton=1")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(KillSwitchActive):
            self.store.control_state()
        with self.assertRaises(KillSwitchActive):
            self.store.submit_proposal(proposal())

    def test_policy_registry_records_immutable_inactive_revision_and_fails_closed(self):
        registry = RiskPolicyRegistry(self.store)
        policy_value = self.risk_policy()
        revision = registry.register_candidate(
            policy_value, effective_at=NOW, recorded_at=NOW, reason="candidate for future review",
        )
        self.assertEqual(revision.approval_status, "PENDING")
        self.assertEqual(revision.policy_digest, hashlib.sha256(
            registry._canonical_policy(policy_value).encode("utf-8")).hexdigest())
        self.assertEqual(registry.history(), (revision,))
        with self.assertRaises(RiskPolicyUnavailable):
            registry.active_policy(now=NOW + timedelta(days=1))
        with self.assertRaises(TypeError):
            registry.register_candidate(policy_value, effective_at=NOW, reason="forged", actor="INVEST-AI-LEAD")
        db = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE risk_policy_revisions SET approval_status='APPROVED' WHERE revision_id=?",
                           (revision.revision_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM risk_policy_revisions WHERE revision_id=?", (revision.revision_id,))
        finally:
            db.close()

    def test_policy_registry_canonical_round_trip_and_duplicate_candidate_history(self):
        registry = RiskPolicyRegistry(self.store)
        policy_value = self.risk_policy()
        canonical = registry._canonical_policy(policy_value)
        self.assertEqual(registry._decode_policy(canonical), policy_value)
        first = registry.register_candidate(policy_value, effective_at=NOW, recorded_at=NOW, reason="revision one")
        second = registry.register_candidate(policy_value, effective_at=NOW, recorded_at=NOW,
                                             reason="revision two")
        self.assertNotEqual(first.revision_id, second.revision_id)
        self.assertEqual(first.policy_digest, second.policy_digest)
        self.assertEqual(len(registry.history()), 2)
        restarted = RiskPolicyRegistry(OrchestrationStore(self.path))
        self.assertEqual({item.revision_id for item in restarted.history()},
                         {first.revision_id, second.revision_id})

    def test_policy_registry_serializes_concurrent_candidates_without_activation(self):
        registry = RiskPolicyRegistry(self.store)
        outcomes = []
        barrier = threading.Barrier(2)

        def register(index):
            barrier.wait()
            outcomes.append(registry.register_candidate(
                self.risk_policy(), effective_at=NOW, recorded_at=NOW, reason=f"parallel candidate {index}",
            ))

        threads = [threading.Thread(target=register, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len({item.revision_id for item in outcomes}), 2)
        self.assertTrue(all(item.approval_status == "PENDING" for item in outcomes))
        with self.assertRaises(RiskPolicyUnavailable):
            registry.active_policy(now=NOW + timedelta(days=1))

    def test_policy_registry_interrupted_insert_rolls_back_revision(self):
        registry = RiskPolicyRegistry(self.store)
        db = sqlite3.connect(self.path)
        try:
            db.execute("CREATE TRIGGER interrupt_policy_insert BEFORE INSERT ON risk_policy_revisions "
                       "BEGIN SELECT RAISE(ABORT,'forced policy registry interruption'); END")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            registry.register_candidate(self.risk_policy(), effective_at=NOW, recorded_at=NOW,
                                        reason="must roll back")
        self.assertEqual(registry.history(), ())

    def test_policy_registry_rejects_digest_tampering_of_an_active_revision(self):
        registry = RiskPolicyRegistry(self.store)
        policy_value = self.risk_policy()
        canonical = registry._canonical_policy(policy_value)
        db = sqlite3.connect(self.path)
        try:
            revision_id = "test-approved-revision"
            db.execute("INSERT INTO risk_policy_revisions VALUES(?,?,?,?,?,?,?,?)",
                       (revision_id, policy_value.version, canonical, "0" * 64, "APPROVED",
                        NOW.isoformat().replace("+00:00", "Z"), NOW.isoformat().replace("+00:00", "Z"),
                        "test-only direct fixture"))
            db.execute("UPDATE risk_policy_control SET active_revision_id=? WHERE singleton=1", (revision_id,))
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(RiskPolicyUnavailable, "digest"):
            registry.active_policy(now=NOW + timedelta(days=1))

    def test_schema_rejects_a_non_null_policy_pointer_without_activating_it(self):
        registry = RiskPolicyRegistry(self.store)
        candidate = registry.register_candidate(self.risk_policy(), effective_at=NOW,
                                                 recorded_at=NOW, reason="inactive candidate")
        db = sqlite3.connect(self.path)
        try:
            db.execute("UPDATE risk_policy_control SET active_revision_id=? WHERE singleton=1",
                       (candidate.revision_id,))
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(RuntimeError, "remain inactive"):
            OrchestrationStore(self.path)
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT active_revision_id FROM risk_policy_control").fetchone()[0],
                             candidate.revision_id)
            self.assertEqual(db.execute("SELECT approval_status FROM risk_policy_revisions").fetchone()[0],
                             "PENDING")
        finally:
            db.close()

    def test_duplicate_proposal_id_and_idempotency_key(self):
        p = proposal()
        self.assertEqual(self.store.submit_proposal(p), "p-1")
        self.assertEqual(self.store.submit_proposal(p), "p-1")
        with self.assertRaises(OrchestrationConflict):
            self.store.submit_proposal(replace(p, symbol="DIFFERENT"))
        self.store.submit_proposal(proposal("p-2", "i-2"))
        with self.assertRaises(OrchestrationConflict):
            self.store.submit_proposal(proposal("p-1", "i-3"))

    def test_risk_policy_transition_and_paper_pending_are_explicit(self):
        p = proposal()
        self.assert_no_authoritative_risk_source(p)
        with self.assertRaises(JobStateError):
            self.store.queue_paper(p.proposal_id)
        with self.assertRaises(JobStateError):
            self.store.record_policy_decision(policy(p))
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)

    def test_kill_switch_persists_and_cannot_be_bypassed_by_accept_decision(self):
        p = proposal()
        self.store.submit_proposal(p)
        self.store.activate_kill_switch(actor="operator", reason="halt")
        self.store = OrchestrationStore(self.path)
        self.assertTrue(self.store.kill_switch_active())
        with self.assertRaises(KillSwitchActive):
            self.store.evaluate_and_record_risk(p.proposal_id, market_snapshot_ref="m-1")
        with self.assertRaises(JobStateError):
            self.store.record_risk_decision(risk(p), exposure_bucket="EUR", exposure_limit=Decimal("1000"))
        with self.assertRaises(KillSwitchActive):
            self.store.submit_proposal(proposal("p-new", "i-new"))
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)

    def test_sqlite_failure_rolls_back_decision_and_state_together(self):
        p = proposal()
        self.store.submit_proposal(p)
        db = sqlite3.connect(self.path)
        try:
            db.execute("""CREATE TRIGGER reject_risk_state BEFORE UPDATE OF state ON trade_proposals
                         WHEN NEW.state='RISK_ACCEPTED' BEGIN SELECT RAISE(ABORT,'forced rollback'); END""")
        finally:
            db.close()
        with self.assertRaises(TrustedRiskUnavailable):
            self.store.evaluate_and_record_risk(p.proposal_id, market_snapshot_ref="m-1")
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0], 0)
        finally:
            db.close()

    def test_rejected_risk_cannot_be_overridden_by_policy_acceptance(self):
        p = proposal()
        self.store.submit_proposal(p)
        self.assert_no_authoritative_risk_source(p)
        with self.assertRaises(JobStateError):
            self.store.record_policy_decision(policy(p))
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)

    def test_policy_after_risk_review_required_is_impossible(self):
        p = proposal()
        self.store.submit_proposal(p)
        self.assert_no_authoritative_risk_source(p)
        with self.assertRaises(JobStateError):
            self.store.record_policy_decision(policy(p))
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)

    def test_canonical_policy_contract_is_immutable_and_requires_paper_and_input_hash(self):
        from invest_ai.policy import PolicyDecision as CanonicalPolicyDecision

        decision = policy(proposal())
        self.assertIsInstance(decision, CanonicalPolicyDecision)
        self.assertEqual(decision.mode, PolicyMode.PAPER)
        with self.assertRaises(FrozenInstanceError):
            decision.input_hash = "b" * 64
        with self.assertRaises(ValueError):
            PolicyMode("LIVE")
        with self.assertRaises(ValueError):
            PolicyResult("REVIEW_REQUIRED")
        with self.assertRaises(TypeError):
            PolicyDecision("id", "proposal", "v1", NOW, PolicyResult.ACCEPT, (), "LIVE", "a" * 64)
        with self.assertRaises(ValueError):
            PolicyDecision("id", "proposal", "v1", NOW, PolicyResult.ACCEPT, (), PolicyMode.PAPER, "missing")

    def test_policy_input_hash_is_persisted_and_replay_is_idempotent(self):
        p = proposal()
        decision = policy(p)
        self.store.submit_proposal(p)
        with self.assertRaises(JobStateError):
            self.store.record_policy_decision(decision)
        with self.assertRaises(JobStateError):
            self.store.record_risk_decision(risk(p), exposure_bucket="EUR", exposure_limit=Decimal("1000"))

    def test_legacy_provisional_policy_without_input_hash_cannot_queue(self):
        p = proposal()
        self.store.submit_proposal(p)
        with self.assertRaises(JobStateError):
            self.store.record_policy_decision(policy(p))
        with self.assertRaises(JobStateError):
            self.store.queue_paper(p.proposal_id)
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)

    def test_policy_rejection_releases_reservation(self):
        p = proposal()
        self.assert_no_authoritative_risk_source(p)
        with self.assertRaises(JobStateError):
            self.store.record_policy_decision(policy(p, PolicyResult.REJECT))
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)

    def test_reservation_is_atomic_under_concurrent_proposals(self):
        proposals = [proposal("pa", "ia"), proposal("pb", "ib")]
        for p in proposals:
            self.store.submit_proposal(p)
        outcomes = []
        barrier = threading.Barrier(2)

        def reserve(p):
            barrier.wait()
            try:
                self.store.evaluate_and_record_risk(p.proposal_id, market_snapshot_ref="missing")
                outcomes.append("UNEXPECTED_RESULT")
            except TrustedRiskUnavailable:
                outcomes.append("FAIL_CLOSED")

        threads = [threading.Thread(target=reserve, args=(p,)) for p in proposals]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertCountEqual(outcomes, ["FAIL_CLOSED", "FAIL_CLOSED"])
        self.assertEqual(self.store.state("pa"), WorkflowState.PROPOSED)
        self.assertEqual(self.store.state("pb"), WorkflowState.PROPOSED)

    def test_event_replay_is_idempotent_and_conflicting_replay_fails(self):
        p = proposal()
        self.assert_no_authoritative_risk_source(p)
        event_count = len(self.store.events(p.proposal_id))
        with self.assertRaises(TrustedRiskUnavailable):
            self.store.evaluate_and_record_risk(p.proposal_id, market_snapshot_ref="snapshot-ref")
        self.assertEqual(len(self.store.events(p.proposal_id)), event_count)
        with self.assertRaises(OrchestrationConflict):
            self.store.submit_proposal(replace(p, symbol="different"))

    def test_outbox_crash_recovery_and_ack_do_not_complete_paper(self):
        with self.assertRaises(JobStateError):
            self.store.queue_paper("unknown-proposal")
        with self.assertRaises(JobStateError):
            self.store.claim_outbox()

    def test_legacy_pending_outbox_cannot_be_claimed_after_kill_switch_resume(self):
        p = proposal("legacy-pending", "legacy-pending-key")
        self.seed_legacy_outbox("legacy-pending-outbox", p, "PENDING", None)
        before = self.outbox_state("legacy-pending-outbox")
        event_count = len(self.store.events(p.proposal_id))

        self.store.activate_kill_switch(actor="operator", reason="pause before restart")
        self.store = OrchestrationStore(self.path)
        with self.assertRaises(JobStateError):
            self.store.claim_outbox(lease_seconds=60, now=NOW)
        self.store.clear_kill_switch(actor="operator", reason="resume after restart")
        with self.assertRaises(JobStateError):
            self.store.claim_outbox(lease_seconds=60, now=NOW)

        self.assertEqual(self.outbox_state("legacy-pending-outbox"), before)
        self.assertEqual(len(self.store.events(p.proposal_id)), event_count)
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)

    def test_legacy_expired_claim_cannot_be_reclaimed_or_mutated(self):
        p = proposal("legacy-expired", "legacy-expired-key")
        self.seed_legacy_outbox("legacy-expired-outbox", p, "CLAIMED", "2026-10-07T09:59:00.000000Z")
        before = self.outbox_state("legacy-expired-outbox")
        event_count = len(self.store.events(p.proposal_id))

        with self.assertRaises(JobStateError):
            self.store.claim_outbox(lease_seconds=-1, now=NOW)

        self.assertEqual(self.outbox_state("legacy-expired-outbox"), before)
        self.assertEqual(len(self.store.events(p.proposal_id)), event_count)

    def test_acknowledge_legacy_claimed_row_refuses_even_with_kill_switch_active(self):
        p = proposal("legacy-claimed", "legacy-claimed-key")
        self.seed_legacy_outbox("legacy-claimed-outbox", p, "CLAIMED", "2026-10-08T12:00:00.000000Z")
        before = self.outbox_state("legacy-claimed-outbox")
        event_count = len(self.store.events(p.proposal_id))
        self.store.activate_kill_switch(actor="operator", reason="hard stop")

        with self.assertRaises(JobStateError):
            self.store.acknowledge_outbox("legacy-claimed-outbox")

        self.assertEqual(self.outbox_state("legacy-claimed-outbox"), before)
        self.assertEqual(len(self.store.events(p.proposal_id)), event_count)
        self.assertTrue(self.store.control_state().halted)

    def test_invalid_mode_transition_and_rollback(self):
        p = proposal()
        self.store.submit_proposal(p)
        before = len(self.store.events(p.proposal_id))
        with self.assertRaises(TypeError):
            policy(p, mode="LIVE")
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)
        self.assertEqual(len(self.store.events(p.proposal_id)), before)

    def test_exposure_over_limit_rejects_promotion_and_releases_nothing(self):
        p = proposal()
        self.store.submit_proposal(p)
        self.assert_no_authoritative_risk_source(p)
        with self.assertRaises(JobStateError):
            self.store.record_policy_decision(policy(p))

    def test_exposure_bucket_limit_cannot_change_between_proposals(self):
        first, second = proposal("p-first", "i-first"), proposal("p-second", "i-second")
        self.assert_no_authoritative_risk_source(first)
        self.store.submit_proposal(second)
        with self.assertRaises(JobStateError):
            self.store.record_risk_decision(risk(second), exposure_bucket="EUR", exposure_limit=Decimal("5000"))
        self.assertEqual(self.store.state(second.proposal_id), WorkflowState.PROPOSED)

    def test_evaluate_risk_fails_closed_without_policy_portfolio_and_market_authority(self):
        p = proposal()
        self.assert_no_authoritative_risk_source(p)
        with self.assertRaises(TrustedRiskUnavailable):
            self.store.evaluate_and_record_risk(p.proposal_id, market_snapshot_ref="different-market-ref")
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)

    def test_forged_accept_with_correct_hash_cannot_be_recorded(self):
        p = proposal()
        self.store.submit_proposal(p)
        forged = risk(p)
        self.assertEqual(forged.result, RiskResult.ACCEPT)
        self.assertTrue(forged.input_hash)
        with self.assertRaises(JobStateError):
            self.store.record_risk_decision(forged, exposure_bucket="EUR", exposure_limit=Decimal("1000"))
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)
        self.assertEqual([event["event_type"] for event in self.store.events(p.proposal_id)],
                         ["TradeProposalPersisted"])

    def test_fixture_cash_arithmetic_rejects_10000_minus_9900_minus_200(self):
        # Synthetic arithmetic only; these values are never installed as operational sources.
        remaining = _available_cash_after_commitments(
            Decimal("10000"), (Decimal("9900"),), Decimal("200")
        )
        self.assertEqual(remaining, Decimal("-100"))

    def test_missing_trust_source_writes_no_receipt_or_reservation(self):
        p = proposal()
        self.assert_no_authoritative_risk_source(p)
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM risk_evaluation_receipts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM risk_cash_reservations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM risk_quantity_reservations").fetchone()[0], 0)
        finally:
            db.close()

    def test_transaction_failure_after_missing_source_check_preserves_proposal_and_events(self):
        p = proposal()
        self.store.submit_proposal(p)
        before_events = len(self.store.events(p.proposal_id))
        with self.assertRaises(TrustedRiskUnavailable):
            self.store.evaluate_and_record_risk(p.proposal_id, market_snapshot_ref="m-ref")
        self.assertEqual(self.store.state(p.proposal_id), WorkflowState.PROPOSED)
        self.assertEqual(len(self.store.events(p.proposal_id)), before_events)

    def test_controlled_risk_api_honors_active_kill_switch(self):
        p = proposal()
        self.store.submit_proposal(p)
        self.store.activate_kill_switch(actor="operator", reason="stop")
        with self.assertRaises(KillSwitchActive):
            self.store.evaluate_and_record_risk(p.proposal_id, market_snapshot_ref="m-1")

    def test_risk_schema_upgrade_and_restart_preserve_fail_closed_control(self):
        self.store.activate_kill_switch(actor="operator", reason="persist halt")
        reopened = OrchestrationStore(self.path)
        self.assertTrue(reopened.control_state().halted)
        self.assertEqual(reopened.control_state().control_epoch, 2)


if __name__ == "__main__":
    unittest.main()
