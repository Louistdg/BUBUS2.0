"""Independent security checks for the fail-closed Risk receipt foundation.

All SQLite state is temporary synthetic fixture data. In particular, the
outbox cases emulate legacy rows and do not connect or dispatch PAPER orders.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "core"))
sys.path.insert(0, str(ROOT / "tests" / "risk"))

from invest_ai.core.errors import JobStateError
from invest_ai.core.orchestration import (
    KillSwitchActive,
    OrchestrationConflict,
    OrchestrationStore,
    TrustedRiskUnavailable,
    WorkflowState,
)
from invest_ai.core.risk_policy import RiskPolicyRegistry, RiskPolicyUnavailable
from invest_ai.risk import RiskResult, compute_input_hash, evaluate
from test_orchestration import NOW, policy, proposal, risk
from test_engine import fixtures as risk_fixtures

AUDIT_NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


class FaultingMigrationStore(OrchestrationStore):
    """SQLite authorizer that denies one selected migration instruction."""

    def __init__(self, path: Path, action: int, name: str | None = None):
        self.audit_action = action
        self.audit_name = name
        super().__init__(path)

    def _connect(self):
        db = super()._connect()

        def authorizer(action, arg1, arg2, database, trigger):
            if action == self.audit_action and (
                self.audit_name is None or self.audit_name in {arg1, arg2}
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorizer)
        return db


class RiskTrustedReceiptFailclosedAudit01(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _store(self, name: str = "runtime.sqlite3") -> OrchestrationStore:
        store = OrchestrationStore(Path(self.temp.name) / name)
        store.clear_kill_switch(actor="audit", reason="temporary test setup")
        return store

    @staticmethod
    def _counts(path: Path, proposal_id: str) -> dict[str, int | str]:
        with closing(sqlite3.connect(path)) as db:
            state = db.execute("SELECT state FROM trade_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            values = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in (
                          "risk_decisions", "risk_evaluation_receipts", "policy_decisions",
                          "risk_cash_reservations", "risk_quantity_reservations",
                          "orchestration_outbox", "orchestration_events",
                      )}
            values["state"] = state[0] if state else "MISSING"
            return values

    def _insert_legacy_outbox(self, store: OrchestrationStore, proposal_id: str, outbox_id: str,
                              status: str, *, created_at: str, lease_until: str | None = None) -> None:
        with closing(sqlite3.connect(store.path)) as db:
            db.execute(
                "INSERT INTO orchestration_outbox VALUES(?,?,?,?,?,?,?,?)",
                (outbox_id, proposal_id, "LegacyPaperRequested",
                 '{"mode":"PAPER","legacy":true}', status, created_at, lease_until, None),
            )
            db.commit()

    @staticmethod
    def _downgrade_to_legacy_schema(path: Path, version: int) -> int:
        """Build v1/v2 migration fixtures while retaining ordinary outbox rows."""
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            for table in ("risk_cash_reservations", "risk_quantity_reservations", "risk_evaluation_receipts"):
                db.execute(f"DROP TABLE {table}")
            db.execute("UPDATE orchestration_meta SET version=?", (version,))
            if version == 1:
                db.execute("ALTER TABLE orchestration_control DROP COLUMN control_epoch")
            db.commit()
            epoch = None
            if version == 2:
                epoch = db.execute("SELECT control_epoch FROM orchestration_control WHERE singleton=1").fetchone()[0]
        return epoch if epoch is not None else 0

    def test_caller_supplied_forged_accept_with_correct_looking_hash_is_never_persisted(self):
        store = self._store()
        p = proposal("forged-accept", "forged-idem")
        store.submit_proposal(p)
        source_proposal, portfolio, market, risk_policy, runtime = risk_fixtures()
        hash_proposal = replace(
            source_proposal, proposal_id=p.proposal_id, idempotency_key=p.idempotency_key,
            market_snapshot_id=p.market_snapshot_id, portfolio_snapshot_id=p.portfolio_snapshot_id,
        )
        actual = evaluate(hash_proposal, portfolio, market, risk_policy, runtime, NOW)
        exact_hash = compute_input_hash(hash_proposal, portfolio, market, risk_policy, runtime, NOW)
        self.assertEqual(actual.input_hash, exact_hash)
        forged = replace(actual, result=RiskResult.ACCEPT, violations=())
        self.assertEqual(forged.result, RiskResult.ACCEPT)
        self.assertEqual(forged.input_hash, exact_hash)
        before = self._counts(store.path, p.proposal_id)
        with self.assertRaises(JobStateError):
            store.record_risk_decision(forged, exposure_bucket="EUR", exposure_limit=1)
        self.assertEqual(self._counts(store.path, p.proposal_id), before)

    def test_evaluate_and_record_risk_refuses_unknown_invalid_halted_and_missing_sources_without_writes(self):
        # Unknown proposal: fail before any workflow or ledger mutation.
        store = self._store("unknown.sqlite3")
        before_global = self._counts(store.path, "missing")
        with self.assertRaises(JobStateError):
            store.evaluate_and_record_risk("missing", market_snapshot_ref="synthetic-market")
        self.assertEqual(self._counts(store.path, "missing"), before_global)

        # A persisted payload whose digest no longer matches must not reach source resolution.
        p_bad = proposal("bad-hash", "bad-hash-idem")
        store.submit_proposal(p_bad)
        with closing(sqlite3.connect(store.path)) as db:
            db.execute("UPDATE trade_proposals SET payload_json=? WHERE proposal_id=?", ("{}", p_bad.proposal_id))
            db.commit()
        before_bad = self._counts(store.path, p_bad.proposal_id)
        with self.assertRaises(OrchestrationConflict):
            store.evaluate_and_record_risk(p_bad.proposal_id, market_snapshot_ref="synthetic-market")
        self.assertEqual(self._counts(store.path, p_bad.proposal_id), before_bad)

        # Halt takes precedence over all untrusted/source inputs.
        p_halt = proposal("halted-proposal", "halted-idem")
        store.clear_kill_switch(actor="audit", reason="test setup")
        store.submit_proposal(p_halt)
        store.activate_kill_switch(actor="audit", reason="audit veto")
        before_halt = self._counts(store.path, p_halt.proposal_id)
        with self.assertRaises(KillSwitchActive):
            store.evaluate_and_record_risk(p_halt.proposal_id, market_snapshot_ref="synthetic-market")
        self.assertEqual(self._counts(store.path, p_halt.proposal_id), before_halt)

        # Without halt, missing policy/portfolio/provider authorities still write nothing.
        store.clear_kill_switch(actor="audit", reason="test resume")
        p_missing = proposal("missing-sources", "missing-sources-idem")
        store.submit_proposal(p_missing)
        before_missing = self._counts(store.path, p_missing.proposal_id)
        with self.assertRaises(TrustedRiskUnavailable):
            store.evaluate_and_record_risk(p_missing.proposal_id, market_snapshot_ref="synthetic-market")
        self.assertEqual(self._counts(store.path, p_missing.proposal_id), before_missing)

    def test_policy_and_paper_write_apis_remain_closed(self):
        store = self._store()
        p = proposal("policy-paper-closed", "policy-paper-idem")
        store.submit_proposal(p)
        before = self._counts(store.path, p.proposal_id)
        with self.assertRaises(JobStateError):
            store.record_policy_decision(policy(p))
        with self.assertRaises(JobStateError):
            store.queue_paper(p.proposal_id)
        self.assertEqual(self._counts(store.path, p.proposal_id), before)
        self.assertEqual(store.state(p.proposal_id), WorkflowState.PROPOSED)

    def test_v1_v2_to_v3_migration_faults_rollback_and_restart_preserves_outbox_and_control(self):
        fault_points = (
            (1, sqlite3.SQLITE_CREATE_TABLE, "risk_evaluation_receipts", "ddl"),
            (1, sqlite3.SQLITE_ALTER_TABLE, "orchestration_control", "alter"),
            (2, sqlite3.SQLITE_UPDATE, "orchestration_meta", "version"),
            (2, sqlite3.SQLITE_TRANSACTION, "COMMIT", "commit"),
        )
        for version, action, name, label in fault_points:
            with self.subTest(fault=label, version=version):
                path = Path(self.temp.name) / f"migration-{version}-{label}.sqlite3"
                store = OrchestrationStore(path)
                store.clear_kill_switch(actor="audit", reason="seed legacy test")
                store.activate_kill_switch(actor="audit", reason="legacy halted state")
                legacy_epoch = self._downgrade_to_legacy_schema(path, version)
                p1, p2 = proposal(f"pending-{label}", f"pending-i-{label}"), proposal(
                    f"claimed-{label}", f"claimed-i-{label}")
                # Proposals are seeded while temporarily resumed, then the legacy halt is restored.
                resumed = OrchestrationStore(path)
                resumed.clear_kill_switch(actor="audit", reason="seed outbox")
                resumed.submit_proposal(p1)
                resumed.submit_proposal(p2)
                resumed.activate_kill_switch(actor="audit", reason="legacy halted state")
                self._insert_legacy_outbox(resumed, p1.proposal_id, f"pending-outbox-{label}", "PENDING",
                                           created_at="2026-10-08T10:00:00.000000Z")
                self._insert_legacy_outbox(resumed, p2.proposal_id, f"claimed-outbox-{label}", "CLAIMED",
                                           created_at="2026-10-08T10:01:00.000000Z",
                                           lease_until="2026-10-08T13:00:00.000000Z")
                # Return to the requested old schema after adding legacy rows.
                legacy_epoch = self._downgrade_to_legacy_schema(path, version)

                with self.assertRaises(sqlite3.DatabaseError):
                    FaultingMigrationStore(path, action, name)

                with closing(sqlite3.connect(path)) as db:
                    current_version = db.execute("SELECT version FROM orchestration_meta").fetchone()[0]
                    self.assertEqual(current_version, version)
                    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    self.assertNotIn("risk_evaluation_receipts", tables)
                    self.assertNotIn("risk_cash_reservations", tables)
                    self.assertNotIn("risk_quantity_reservations", tables)
                    control_columns = {r[1] for r in db.execute("PRAGMA table_info(orchestration_control)")}
                    if version == 1:
                        self.assertNotIn("control_epoch", control_columns)
                    else:
                        self.assertEqual(db.execute("SELECT control_epoch FROM orchestration_control").fetchone()[0], legacy_epoch)
                    self.assertEqual(db.execute("SELECT halted FROM orchestration_control").fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM orchestration_outbox").fetchone()[0], 2)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM trade_proposals").fetchone()[0], 2)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0], 0)
                recovered = OrchestrationStore(path)
                self.assertTrue(recovered.kill_switch_active())
                self.assertEqual(recovered.control_state().control_epoch, 0 if version == 1 else legacy_epoch)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("SELECT version FROM orchestration_meta").fetchone()[0], 4)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM orchestration_outbox").fetchone()[0], 2)
                with self.assertRaises(RiskPolicyUnavailable):
                    RiskPolicyRegistry(recovered).active_policy(now=NOW + timedelta(days=1))

    def test_receipt_immutability_triggers_exist_and_reject_mutation_of_synthetic_fixture(self):
        store = self._store()
        p = proposal("receipt-trigger", "receipt-trigger-idem")
        store.submit_proposal(p)
        with closing(sqlite3.connect(store.path)) as db:
            trigger_names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertIn("risk_evaluation_receipts_no_update", trigger_names)
            self.assertIn("risk_evaluation_receipts_no_delete", trigger_names)
            db.execute(
                "INSERT INTO risk_evaluation_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("synthetic-receipt", p.proposal_id, "req", "prop", "policy-rev", "policy-digest",
                 "portfolio-snapshot", 1, "portfolio-digest", "market-snapshot", 1, "market-digest",
                 "{}", "decision-digest", 0, "2026-10-08T12:00:00Z"),
            )
            db.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE risk_evaluation_receipts SET decision_digest='changed' WHERE receipt_id=?",
                           ("synthetic-receipt",))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM risk_evaluation_receipts WHERE receipt_id=?", ("synthetic-receipt",))
        # A manually inserted test row is not an authoritative positive receipt.
        self.assertEqual(store.state(p.proposal_id), WorkflowState.PROPOSED)

    def test_legacy_pending_outbox_is_never_claimed_or_acknowledged_across_kill_epoch_and_restart(self):
        store = self._store()
        p = proposal("legacy-pending", "legacy-pending-idem")
        store.submit_proposal(p)
        self._insert_legacy_outbox(store, p.proposal_id, "legacy-pending-row", "PENDING",
                                   created_at="2026-10-08T10:00:00.000000Z")
        with closing(sqlite3.connect(store.path)) as db:
            outbox_columns = {row[1] for row in db.execute("PRAGMA table_info(orchestration_outbox)")}
            self.assertNotIn("control_epoch", outbox_columns)
            self.assertNotIn("receipt_id", outbox_columns)

        for halted in (False, True):
            if halted:
                store.activate_kill_switch(actor="audit", reason="halt before legacy claim")
            else:
                store.clear_kill_switch(actor="audit", reason="resume legacy claim")
            epoch = store.control_state().control_epoch
            self._assert_outbox_api_refuses_without_database_access(
                store, self._api_call(store, "claim"), p.proposal_id,
            )
            self.assertEqual(store.control_state().control_epoch, epoch)
            self._assert_outbox_api_refuses_without_database_access(
                store, self._api_call(store, "acknowledge"), p.proposal_id,
            )
            self.assertEqual(store.control_state().control_epoch, epoch)

        # Restart must not revive legacy dispatch or mutate the PENDING row.
        restarted = OrchestrationStore(store.path)
        self._assert_outbox_api_refuses_without_database_access(
            restarted, self._api_call(restarted, "claim"), p.proposal_id,
        )
        self._assert_outbox_api_refuses_without_database_access(
            restarted, self._api_call(restarted, "acknowledge"), p.proposal_id,
        )

    def _assert_outbox_api_refuses_without_database_access(self, store, operation, proposal_id):
        before = self._counts(store.path, proposal_id)
        before_bytes = store.path.read_bytes()

        class NoDatabaseAccessStore(OrchestrationStore):
            def _connect(inner_self):
                raise AssertionError("outbox API attempted to open SQLite")

        guarded = NoDatabaseAccessStore.__new__(NoDatabaseAccessStore)
        guarded.path = store.path
        method = getattr(operation, "_outbox_method", None)
        guarded_operation = None
        if method == "claim":
            guarded_operation = lambda: guarded.claim_outbox(now=AUDIT_NOW)
        elif method == "acknowledge":
            guarded_operation = lambda: guarded.acknowledge_outbox("legacy-pending-row")
        self.assertIsNotNone(guarded_operation)
        with self.assertRaises(JobStateError):
            guarded_operation()
        self.assertEqual(store.path.read_bytes(), before_bytes)
        self.assertEqual(self._counts(store.path, proposal_id), before)

    def test_expired_legacy_claim_is_immutable_across_halt_resume_restart_and_concurrent_claims(self):
        store = self._store()
        old = proposal("legacy-expired", "legacy-expired-idem")
        store.submit_proposal(old)
        self._insert_legacy_outbox(store, old.proposal_id, "legacy-expired-row", "CLAIMED",
                                   created_at="2026-10-08T09:00:00.000000Z",
                                   lease_until="2026-10-08T11:00:00.000000Z")
        store.activate_kill_switch(actor="audit", reason="halt with expired legacy lease")
        store.clear_kill_switch(actor="audit", reason="resume after expiration")
        store.activate_kill_switch(actor="audit", reason="new control epoch")
        epoch = store.control_state().control_epoch
        self._assert_outbox_api_refuses_without_database_access(
            store, self._api_call(store, "claim"), old.proposal_id,
        )
        self._assert_outbox_api_refuses_without_database_access(
            store, self._api_call(store, "acknowledge"), old.proposal_id,
        )
        self.assertEqual(store.control_state().control_epoch, epoch)
        restarted = OrchestrationStore(store.path)
        self._assert_outbox_api_refuses_without_database_access(
            restarted, self._api_call(restarted, "claim"), old.proposal_id,
        )

        outcomes = []
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=5)
            try:
                store.claim_outbox(now=AUDIT_NOW)
                outcomes.append("UNEXPECTED_SUCCESS")
            except JobStateError:
                outcomes.append("REFUSED")

        before_concurrency = store.path.read_bytes()
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(outcomes, ["REFUSED", "REFUSED"])
        self.assertEqual(store.path.read_bytes(), before_concurrency)
        with closing(sqlite3.connect(store.path)) as db:
            row = db.execute("SELECT status,lease_until FROM orchestration_outbox WHERE outbox_id=?",
                             ("legacy-expired-row",)).fetchone()
            self.assertEqual(row[0], "CLAIMED")
            self.assertEqual(row[1], "2026-10-08T11:00:00.000000Z")

    @staticmethod
    def _api_call(store, method):
        def call():
            if method == "claim":
                return store.claim_outbox(now=AUDIT_NOW)
            return store.acknowledge_outbox("legacy-expired-row")
        call._outbox_method = method
        return call

    def test_synthetic_cash_arithmetic_is_not_a_portfolio_or_risk_receipt(self):
        from invest_ai.core.orchestration import _available_cash_after_commitments

        self.assertEqual(
            _available_cash_after_commitments(
                Decimal("10000"), (Decimal("9900"),), Decimal("200"),
            ),
            Decimal("-100"),
        )
        store = self._store()
        p = proposal("cash-not-authority", "cash-not-authority-idem")
        store.submit_proposal(p)
        before = self._counts(store.path, p.proposal_id)
        with self.assertRaises(TrustedRiskUnavailable):
            store.evaluate_and_record_risk(p.proposal_id, market_snapshot_ref="synthetic-only")
        self.assertEqual(self._counts(store.path, p.proposal_id), before)
        self.assertEqual(store.state(p.proposal_id), WorkflowState.PROPOSED)


if __name__ == "__main__":
    unittest.main()
