"""Independent adversarial checks for A/B/C trust foundations.

Some tests deliberately assert that a documented limitation is reproducible;
their unittest PASS means the reproduction matched the expected behavior.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "data"))
sys.path.insert(0, str(ROOT / "tests" / "portfolio"))
sys.path.insert(0, str(ROOT / "tests" / "core"))
sys.path.insert(0, str(ROOT / "tests" / "integration"))

from invest_ai.core.orchestration import OrchestrationStore
from invest_ai.core.risk_policy import RiskPolicyRegistry, RiskPolicyUnavailable
from invest_ai.core import orchestration as orchestration_module
from invest_ai.data.archive import ArchiveError, NormalizedArchive, RawArchive
from invest_ai.data.market import build_market_snapshot
from invest_ai.data.models import DataProvenance, Instrument, MarketBar, Quote, canonical_json_bytes
from invest_ai.data.providers import RawPayload
from invest_ai.data.snapshot_store import MarketSnapshotStore
from invest_ai.data.snapshot_store import SnapshotStoreError
from invest_ai.portfolio import (
    PortfolioBootstrapConfig, PortfolioContractError, ReservationReference,
    ReservationStatus, compute_bootstrap_config_digest,
    compute_reservation_snapshot_digest, project_portfolio,
)
from invest_ai.risk import (
    AssetType, MarketState, RiskPolicy, RiskResult, RiskRuntimeState, Side,
    TradeProposal, evaluate,
)
from test_projector import NOW as PF_NOW, buy_event, config as pf_config, reservation_view
from test_gateway import AS_OF as RISK_AS_OF


class AuditFaultingOrchestrationStore(OrchestrationStore):
    """Independent SQLite authorizer fault injector for migration checkpoints."""

    def __init__(self, db_path, action: int, name: str | None = None):
        self.audit_action = action
        self.audit_name = name
        super().__init__(db_path)

    def _connect(self):
        db = super()._connect()

        def authorizer(action, arg1, arg2, database, trigger):
            if action == self.audit_action:
                if self.audit_name is None or self.audit_name in {arg1, arg2}:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorizer)
        return db


class TrustedFoundationsAudit01(unittest.TestCase):
    @staticmethod
    def _persist_quote(root: Path, *, dataset: str = "quote", observed: datetime | None = None):
        observed = observed or (PF_NOW - timedelta(seconds=5))
        raw = RawArchive(root).archive(RawPayload(
            provider="audit", dataset=dataset, source="fixture://reaudit-quote",
            record_id="reaudit-quote", body=b"immutable-audit-quote", observed_at=observed,
            published_at=observed, received_at=observed, available_at=observed,
            instrument_id="instrument-1", currency="USD", unit="USD/share",
            media_type="application/json",
        ))
        provenance = DataProvenance(
            provider="audit", source="fixture://reaudit-quote", dataset=dataset,
            instrument_id="instrument-1", observed_at=observed, published_at=observed,
            received_at=observed, available_at=observed, revision=1, currency="USD",
            unit="USD/share", raw_reference=raw.reference, raw_sha256=raw.sha256,
            record_id="reaudit-quote",
        )
        quote = Quote(provenance, Decimal("9.99"), Decimal("10.01"), venue="XNAS")
        instrument = Instrument("instrument-1", "XYZ", "EQUITY", "USD", exchange="XNAS",
                                venue="XNAS", multiplier=Decimal("1"),
                                price_convention="USD_PER_SHARE")
        NormalizedArchive(root).append(quote)
        max_age = timedelta(minutes=5)
        snapshot = build_market_snapshot(quote, instrument, as_of=PF_NOW, max_age=max_age)
        store = MarketSnapshotStore(root)
        record = store.register(snapshot, quote, instrument, max_age=max_age)
        return store, record, raw, quote, max_age

    @staticmethod
    def _make_v1_database(path: Path, *, halted: int = 1) -> None:
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE orchestration_meta(version INTEGER NOT NULL)")
            db.execute("INSERT INTO orchestration_meta VALUES(1)")
            db.execute("CREATE TABLE orchestration_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                       "halted INTEGER NOT NULL CHECK(halted IN (0,1)), changed_at TEXT NOT NULL, "
                       "actor TEXT NOT NULL, reason TEXT NOT NULL)")
            db.execute("INSERT INTO orchestration_control VALUES(1,?,'t','legacy','preserve halt')", (halted,))
            db.commit()

    def test_a_independent_faults_at_ddl_alter_version_and_commit_rollback_and_recover(self):
        fault_points = (
            (sqlite3.SQLITE_CREATE_TABLE, "risk_policy_revisions", "DDL"),
            (sqlite3.SQLITE_ALTER_TABLE, "orchestration_control", "ALTER"),
            (sqlite3.SQLITE_UPDATE, "orchestration_meta", "version"),
            (sqlite3.SQLITE_TRANSACTION, "COMMIT", "commit"),
        )
        with tempfile.TemporaryDirectory() as temp:
            for action, name, label in fault_points:
                with self.subTest(fault=label):
                    path = Path(temp) / f"{label}.sqlite3"
                    self._make_v1_database(path, halted=1)
                    with self.assertRaises(sqlite3.DatabaseError):
                        AuditFaultingOrchestrationStore(path, action, name)
                    with closing(sqlite3.connect(path)) as db:
                        names = {row[0] for row in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'")}
                        columns = {row[1] for row in db.execute(
                            "PRAGMA table_info(orchestration_control)")}
                        self.assertEqual(names, {"orchestration_meta", "orchestration_control"})
                        self.assertNotIn("control_epoch", columns)
                        self.assertEqual(db.execute("SELECT version FROM orchestration_meta").fetchone()[0], 1)
                        self.assertEqual(db.execute("SELECT halted FROM orchestration_control").fetchone()[0], 1)
                    recovered = OrchestrationStore(path)
                    self.assertTrue(recovered.kill_switch_active())
                    self.assertEqual(recovered.control_state().control_epoch, 0)
                    with self.assertRaises(RiskPolicyUnavailable):
                        RiskPolicyRegistry(recovered).active_policy(now=PF_NOW + timedelta(days=1))

    def test_a_unknown_incomplete_and_legacy_partial_schemas_fail_closed_or_resume_safely(self):
        with tempfile.TemporaryDirectory() as temp:
            unknown = Path(temp) / "unknown.sqlite3"
            self._make_v1_database(unknown)
            with closing(sqlite3.connect(unknown)) as db:
                db.execute("UPDATE orchestration_meta SET version=77")
                db.commit()
            with self.assertRaises(RuntimeError):
                OrchestrationStore(unknown)
            with closing(sqlite3.connect(unknown)) as db:
                self.assertEqual(db.execute("SELECT version FROM orchestration_meta").fetchone()[0], 77)
                self.assertEqual(db.execute("SELECT halted FROM orchestration_control").fetchone()[0], 1)
                self.assertNotIn("control_epoch", {r[1] for r in db.execute("PRAGMA table_info(orchestration_control)")})

            incomplete = Path(temp) / "incomplete-v2.sqlite3"
            with closing(sqlite3.connect(incomplete)) as db:
                db.execute("CREATE TABLE orchestration_meta(version INTEGER NOT NULL)")
                db.execute("INSERT INTO orchestration_meta VALUES(3)")
                db.execute("CREATE TABLE orchestration_control(singleton INTEGER PRIMARY KEY, halted INTEGER NOT NULL, "
                           "changed_at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, control_epoch INTEGER NOT NULL)")
                db.execute("INSERT INTO orchestration_control VALUES(1,1,'t','legacy','halted',9)")
                db.commit()
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                OrchestrationStore(incomplete)
            with closing(sqlite3.connect(incomplete)) as db:
                self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                                 {"orchestration_meta", "orchestration_control"})
                self.assertEqual(db.execute("SELECT control_epoch FROM orchestration_control").fetchone()[0], 9)

            partial = Path(temp) / "legacy-partial.sqlite3"
            self._make_v1_database(partial, halted=1)
            with closing(sqlite3.connect(partial)) as db:
                db.execute("CREATE TABLE risk_policy_revisions(revision_id TEXT PRIMARY KEY)")
                db.commit()
            with self.assertRaises(RuntimeError):
                OrchestrationStore(partial)
            with closing(sqlite3.connect(partial)) as db:
                self.assertEqual(db.execute("SELECT version FROM orchestration_meta").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT halted FROM orchestration_control").fetchone()[0], 1)
                self.assertNotIn("control_epoch", {r[1] for r in db.execute("PRAGMA table_info(orchestration_control)")})

            resumable = Path(temp) / "legacy-partially-created.sqlite3"
            with closing(sqlite3.connect(resumable)) as db:
                db.execute("CREATE TABLE orchestration_meta(version INTEGER NOT NULL)")
                db.execute("INSERT INTO orchestration_meta VALUES(1)")
                db.execute("CREATE TABLE orchestration_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                           "halted INTEGER NOT NULL CHECK(halted IN (0,1)), changed_at TEXT NOT NULL, "
                           "actor TEXT NOT NULL, reason TEXT NOT NULL, control_epoch INTEGER NOT NULL DEFAULT 0)")
                db.execute("INSERT INTO orchestration_control VALUES(1,1,'t','legacy','preserve halt',17)")
                for statement in orchestration_module._SCHEMA_STATEMENTS:
                    db.execute(statement)
                db.commit()
            recovered_partial = OrchestrationStore(resumable)
            state = recovered_partial.control_state()
            self.assertTrue(state.halted)
            self.assertEqual(state.control_epoch, 17)
            with closing(sqlite3.connect(resumable)) as db:
                self.assertEqual(db.execute("SELECT version FROM orchestration_meta").fetchone()[0], 4)
            with self.assertRaises(RiskPolicyUnavailable):
                RiskPolicyRegistry(recovered_partial).active_policy(now=PF_NOW + timedelta(days=1))

    def test_a_concurrent_control_transitions_keep_epoch_monotone(self):
        with tempfile.TemporaryDirectory() as temp:
            store = OrchestrationStore(Path(temp) / "runtime.sqlite3")
            initial = store.control_state().control_epoch
            barrier = threading.Barrier(8)
            errors = []

            def activate(index):
                try:
                    barrier.wait(timeout=5)
                    store.activate_kill_switch(actor=f"audit-{index}", reason="concurrent audit")
                except Exception as exc:  # surfaced below as an audit failure
                    errors.append(exc)

            threads = [threading.Thread(target=activate, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(errors, errors)
            state = store.control_state()
            self.assertTrue(state.halted)
            self.assertEqual(state.control_epoch, initial + 8)

    def test_a_interrupted_v1_migration_rolls_back_all_ddl_and_recovers(self):
        """Historical finding: DDL used to leak; corrected migration is atomic."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.sqlite3"
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE orchestration_meta(version INTEGER NOT NULL)")
                db.execute("INSERT INTO orchestration_meta VALUES(1)")
                db.execute("CREATE TABLE orchestration_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                           "halted INTEGER NOT NULL CHECK(halted IN (0,1)), changed_at TEXT NOT NULL, "
                           "actor TEXT NOT NULL, reason TEXT NOT NULL)")
                db.execute("INSERT INTO orchestration_control VALUES(1,1,'t','legacy','halted')")
                db.execute("CREATE TRIGGER interrupt_migration BEFORE UPDATE ON orchestration_meta "
                           "BEGIN SELECT RAISE(ABORT,'audit interrupted migration'); END")
                db.commit()
            finally:
                db.close()
            with self.assertRaises(sqlite3.IntegrityError):
                OrchestrationStore(path)
            db = sqlite3.connect(path)
            try:
                names = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                columns = {row[1] for row in db.execute("PRAGMA table_info(orchestration_control)")}
                self.assertEqual(names, {"orchestration_meta", "orchestration_control"})
                self.assertNotIn("control_epoch", columns)
                self.assertEqual(db.execute("SELECT version FROM orchestration_meta").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT halted FROM orchestration_control").fetchone()[0], 1)
            finally:
                db.close()
            with closing(sqlite3.connect(path)) as db:
                db.execute("DROP TRIGGER interrupt_migration")
                db.commit()
            resumed = OrchestrationStore(path)
            self.assertTrue(resumed.kill_switch_active())
            self.assertEqual(resumed.control_state().control_epoch, 0)
            with self.assertRaises(RiskPolicyUnavailable):
                RiskPolicyRegistry(resumed).active_policy(now=PF_NOW + timedelta(days=1))

    def test_a_policy_same_version_different_content_is_separate_and_inactive(self):
        """No active policy can be resolved; same label does not collapse digests."""
        with tempfile.TemporaryDirectory() as temp:
            store = OrchestrationStore(Path(temp) / "runtime.sqlite3")
            registry = RiskPolicyRegistry(store)
            from test_orchestration import OrchestrationTests
            policy_one = OrchestrationTests.risk_policy()
            policy_two = replace(policy_one, max_order_notional=Decimal("4999"))
            first = registry.register_candidate(policy_one, effective_at=PF_NOW,
                                                recorded_at=PF_NOW, reason="audit candidate one")
            second = registry.register_candidate(policy_two, effective_at=PF_NOW,
                                                 recorded_at=PF_NOW, reason="audit candidate two")
            self.assertEqual(first.policy_version, second.policy_version)
            self.assertNotEqual(first.policy_digest, second.policy_digest)
            self.assertEqual(first.approval_status, "PENDING")
            self.assertEqual(second.approval_status, "PENDING")
            with self.assertRaises(RiskPolicyUnavailable):
                registry.active_policy(now=PF_NOW + timedelta(days=1))

    def test_b_marketbar_labeled_quote_is_refused_after_persistence_and_reconstruction(self):
        """Historical bypass is closed by reconstructing the persisted observation type."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            observed = PF_NOW - timedelta(minutes=1)
            raw = RawArchive(root).archive(RawPayload(
                provider="audit", dataset="quote", source="fixture://bar-labeled-quote",
                record_id="bar-disguised", body=b"local audit fixture", observed_at=observed,
                published_at=observed, received_at=observed, available_at=observed,
                instrument_id="instrument-1", currency="USD", unit="USD/share",
                media_type="application/json",
            ))
            provenance = DataProvenance(
                provider="audit", source="fixture://bar-labeled-quote", dataset="quote",
                instrument_id="instrument-1", observed_at=observed, published_at=observed,
                received_at=observed, available_at=observed, revision=1, currency="USD",
                unit="USD/share", raw_reference=raw.reference, raw_sha256=raw.sha256,
                record_id="bar-disguised",
            )
            bar = MarketBar(provenance, "1d", observed - timedelta(days=1), observed,
                            Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"),
                            Decimal("100"), adjusted=False)
            instrument = Instrument("instrument-1", "XYZ", "EQUITY", "USD", exchange="XNAS",
                                    venue="XNAS", multiplier=Decimal("1"),
                                    price_convention="USD_PER_SHARE")
            NormalizedArchive(root).append(bar)
            snapshot = build_market_snapshot(bar, instrument, as_of=PF_NOW,
                                             max_age=timedelta(minutes=5))
            store = MarketSnapshotStore(root)
            record = store.register(snapshot, bar, instrument, max_age=timedelta(minutes=5))
            result = MarketSnapshotStore(root).assess_for_risk(
                record.snapshot_id, as_of=PF_NOW, max_age=timedelta(minutes=5))
            self.assertEqual(type(bar).__name__, "MarketBar")
            self.assertFalse(result.eligible)
            self.assertIn("OBSERVATION_NOT_QUOTE", result.reasons)

    def test_b_real_quote_remains_eligible_after_fresh_store_reconstruction_and_staleness_is_rechecked(self):
        with tempfile.TemporaryDirectory() as temp:
            _store, record, _raw, _quote, _max_age = self._persist_quote(Path(temp))
            fresh = MarketSnapshotStore(Path(temp))
            assessment = fresh.assess_for_risk(
                record.snapshot_id, as_of=PF_NOW, max_age=timedelta(minutes=5))
            self.assertTrue(assessment.eligible, assessment.reasons)
            stale = fresh.assess_for_risk(
                record.snapshot_id, as_of=PF_NOW, max_age=timedelta(seconds=2))
            self.assertFalse(stale.eligible)
            self.assertIn("STALE", stale.reasons)

    def test_b_quote_with_noncanonical_dataset_and_tampered_archives_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store, record, raw, _quote, _max_age = self._persist_quote(root, dataset="market_bar")
            mismatch = store.assess_for_risk(
                record.snapshot_id, as_of=PF_NOW, max_age=timedelta(minutes=5))
            self.assertFalse(mismatch.eligible)
            self.assertIn("NOT_A_QUOTE", mismatch.reasons)
            (root / raw.reference).write_bytes(b"tampered raw bytes")
            with self.assertRaises(SnapshotStoreError):
                MarketSnapshotStore(root).get(record.snapshot_id)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _store, record, _raw, _quote, _max_age = self._persist_quote(root)
            (root / record.normalized_reference).write_bytes(bytes([123, 125]))
            with self.assertRaises(SnapshotStoreError):
                MarketSnapshotStore(root).get(record.snapshot_id)

    def test_b_concurrent_identical_snapshot_registration_is_idempotent(self):
        """Concurrent identical registrations preserve content and produce a readable record."""
        from unittest.mock import patch
        import invest_ai.data.snapshot_store as snapshot_store_module
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            observed = PF_NOW - timedelta(minutes=1)
            raw = RawArchive(root).archive(RawPayload(
                provider="audit", dataset="quote", source="fixture://race",
                record_id="race", body=b"race-fixture", observed_at=observed,
                published_at=observed, received_at=observed, available_at=observed,
                instrument_id="instrument-1", currency="USD", unit="USD/share",
                media_type="application/json",
            ))
            provenance = DataProvenance(
                provider="audit", source="fixture://race", dataset="quote",
                instrument_id="instrument-1", observed_at=observed, published_at=observed,
                received_at=observed, available_at=observed, revision=1, currency="USD",
                unit="USD/share", raw_reference=raw.reference, raw_sha256=raw.sha256,
                record_id="race",
            )
            quote = __import__("invest_ai.data.models", fromlist=["Quote"]).Quote(
                provenance, Decimal("9.99"), Decimal("10.01"), venue="XNAS")
            instrument = Instrument("instrument-1", "XYZ", "EQUITY", "USD", exchange="XNAS",
                                    venue="XNAS", multiplier=Decimal("1"),
                                    price_convention="USD_PER_SHARE")
            from invest_ai.data.models import canonical_json_bytes
            normalized = root / "data" / "normalized" / "audit" / "quote" / f"{provenance.observation_id}.json"
            normalized.parent.mkdir(parents=True, exist_ok=True)
            normalized.write_bytes(canonical_json_bytes(quote))
            store = MarketSnapshotStore(root)
            snapshot = build_market_snapshot(quote, instrument, as_of=PF_NOW,
                                             max_age=timedelta(minutes=5))
            barrier = threading.Barrier(2)
            original_replace = snapshot_store_module.os.replace

            def synchronized_replace(source, target):
                barrier.wait(timeout=5)
                return original_replace(source, target)

            outcomes = []

            def register():
                try:
                    outcomes.append(store.register(snapshot, quote, instrument,
                                                   max_age=timedelta(minutes=5)))
                except Exception as exc:
                    outcomes.append(exc)

            with patch.object(snapshot_store_module.os, "replace", synchronized_replace):
                threads = [threading.Thread(target=register) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
            records = [item for item in outcomes if not isinstance(item, Exception)]
            failures = [item for item in outcomes if isinstance(item, Exception)]
            self.assertGreaterEqual(len(records), 1, outcomes)
            self.assertLessEqual(len(records), 2, outcomes)
            self.assertTrue(all(isinstance(item, (SnapshotStoreError, ArchiveError)) for item in failures), outcomes)
            self.assertTrue(all(item.snapshot_sha256 == records[0].snapshot_sha256 for item in records))
            self.assertEqual(store.get(snapshot.snapshot_id).snapshot, snapshot)
            eligible = MarketSnapshotStore(root).assess_for_risk(
                snapshot.snapshot_id, as_of=PF_NOW, max_age=timedelta(minutes=5))
            self.assertTrue(eligible.eligible, eligible.reasons)

    def test_c_bootstrap_accepts_self_consistent_unverified_approval_reference_but_marks_fixture_only(self):
        """Content hashes do not authenticate capital approval; output stays fenced."""
        approval_digest = "a" * 64
        config_digest = compute_bootstrap_config_digest(
            "audit-portfolio", "EUR", Decimal("123456"),
            "UNVERIFIED_CALLER_APPROVAL", approval_digest,
        )
        candidate = PortfolioBootstrapConfig(
            "audit-portfolio", "EUR", Decimal("123456"),
            "UNVERIFIED_CALLER_APPROVAL", approval_digest, config_digest,
        )
        result = project_portfolio(candidate, (), reservations=reservation_view(), as_of=PF_NOW)
        self.assertEqual(result.initial_cash, Decimal("123456"))
        self.assertEqual(result.authority_status, "FIXTURE_PROJECTION_ONLY")
        with self.assertRaises(PortfolioContractError):
            replace(result, authority_status="OPERATIONAL")

    def test_c_each_missing_reservation_unit_fails_risk_mapping(self):
        reservations = (
            ReservationReference("proposal-no-instrument", Decimal("50"), "bucket",
                                 ReservationStatus.RESERVED, PF_NOW, currency="EUR"),
            ReservationReference("proposal-no-currency", Decimal("60"), "bucket",
                                 ReservationStatus.RESERVED, PF_NOW, instrument_id="instrument-2"),
        )
        digest = compute_reservation_snapshot_digest("audit-reservations", 1, reservations)
        from invest_ai.portfolio import ReservationSnapshot
        view = ReservationSnapshot("audit-reservations", 1, reservations, digest)
        result = project_portfolio(pf_config(), (), reservations=view, as_of=PF_NOW)
        for item in result.pending_exposures:
            with self.subTest(proposal=item.proposal_id):
                with self.assertRaises(PortfolioContractError):
                    result.to_risk_state()

    def test_c_portfolio_values_apply_multiplier_and_are_reproducible(self):
        leveraged_fill = replace(buy_event(), multiplier=Decimal("10"))
        first = project_portfolio(pf_config(initial_cash=Decimal("5000")), (leveraged_fill,),
                                  reservations=reservation_view(), as_of=PF_NOW + timedelta(minutes=1))
        second = project_portfolio(pf_config(initial_cash=Decimal("5000")), (leveraged_fill,),
                                   reservations=reservation_view(), as_of=PF_NOW + timedelta(minutes=1))
        self.assertEqual(first, second)
        self.assertEqual(first.cash, Decimal("2979.00"))
        self.assertEqual(first.positions[0].market_value, Decimal("2000"))
        self.assertEqual(first.equity, Decimal("4979.00"))
        self.assertEqual(first.total_pnl, Decimal("-21.00"))

    def test_c_risk_cash_check_ignores_existing_pending_cash_commitment(self):
        """A fully typed 9,900 reservation plus a new 200 buy exceeds 10,000 cash."""
        pending = ReservationReference(
            "pending-proposal", Decimal("9900"), "EUR", ReservationStatus.RESERVED,
            RISK_AS_OF - timedelta(minutes=1), instrument_id="instrument-1",
            currency="EUR", idempotency_key="pending-key",
        )
        reservations = reservation_view(pending, revision=2)
        snapshot = project_portfolio(pf_config(initial_cash=Decimal("10000")), (),
                                     reservations=reservations, as_of=RISK_AS_OF)
        portfolio = snapshot.to_risk_state()
        market = MarketState("market-risk", "instrument-1", RISK_AS_OF - timedelta(seconds=10),
                             Decimal("10"), True, "EUR", "XNAS", Decimal("1"),
                             Decimal("0.01"), Decimal("1"), "industrials")
        proposal = TradeProposal(
            "new-proposal", "new-key", "instrument-1", "XYZ", AssetType.EQUITY,
            Side.BUY, Decimal("20"), "audit-strategy", RISK_AS_OF - timedelta(seconds=5),
            stop_price=Decimal("9"), market_snapshot_id="market-risk",
            portfolio_snapshot_id=portfolio.snapshot_id,
        )
        policy = RiskPolicy(
            "audit-policy", frozenset({"instrument-1"}), frozenset({Side.BUY}),
            frozenset({AssetType.EQUITY}), Decimal("1000"), Decimal("100000"),
            Decimal("1"), Decimal("1"), Decimal("100000"), Decimal("1"),
            Decimal("100000"), Decimal("100000"), Decimal("1"), Decimal("100"),
            3600, 3600, 100, 3600, Decimal("0"), Decimal("0"), True,
        )
        decision = evaluate(proposal, portfolio, market, policy, RiskRuntimeState(), RISK_AS_OF)
        self.assertGreater(Decimal("9900") + Decimal("200"), portfolio.cash)
        self.assertEqual(dict(decision.calculated_metrics)["cash_after_reservation"], "9800")
        self.assertIs(decision.result, RiskResult.REJECT)
        self.assertIn("MAX_INSTRUMENT_CONCENTRATION", {item.code for item in decision.violations})


if __name__ == "__main__":
    unittest.main()
