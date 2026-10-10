from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from invest_ai.core.backup import backup_knowledge_database, restore_knowledge_backup
from invest_ai.core.bootstrap import bootstrap_runtime
from invest_ai.core.clock import iso_utc, parse_utc, to_utc, utc_now
from invest_ai.core.config import load_config
from invest_ai.core.errors import AlreadyRunningError, ConfigurationError, JobStateError
from invest_ai.core.health import HealthRegistry, HealthStatus
from invest_ai.core.ids import stable_id
from invest_ai.core.jobs import RuntimeStore
from invest_ai.core.logging import configure_logging
from invest_ai.core.modes import RuntimeMode
from invest_ai.core.paths import RuntimePaths
from invest_ai.core.single_instance import InstanceGuard


class RuntimeCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "INVEST-AI"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_01_valid_config_defaults_and_modes(self) -> None:
        config = load_config(None, self.root, environ={})
        self.assertEqual(config.mode, RuntimeMode.RESEARCH)
        self.assertEqual(load_config(None, self.root, environ={"INVEST_AI_MODE": "paper"}).mode, RuntimeMode.PAPER)

    def test_02_invalid_config_and_live_mode_are_rejected(self) -> None:
        invalid = self.root / "invalid.toml"
        invalid.write_text('[runtime]\nmode="LIVE"\n', encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_config(invalid, self.root, environ={})
        invalid.write_text('[unexpected]\nvalue=1\n', encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_config(invalid, self.root, environ={})
        with self.assertRaises(ConfigurationError):
            load_config(None, self.root, environ={"INVEST_AI_MODE": "LIVE"})

    def test_03_config_rejects_wrong_types_and_unknown_keys(self) -> None:
        invalid = self.root / "invalid.toml"
        invalid.write_text('[jobs]\nlease_seconds="soon"\n', encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_config(invalid, self.root, environ={})
        invalid.write_text('[runtime]\nsecret="must not be accepted"\n', encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_config(invalid, self.root, environ={})

    def test_04_utc_clock_rejects_naive_datetime(self) -> None:
        now = utc_now()
        self.assertEqual(now.utcoffset(), timedelta(0))
        self.assertTrue(iso_utc(now).endswith("Z"))
        self.assertEqual(parse_utc(iso_utc(now)), now)
        with self.assertRaises(ValueError):
            to_utc(datetime(2026, 1, 1))

    def test_05_canonical_paths_are_pure_and_scoped(self) -> None:
        paths = RuntimePaths.from_root(self.root)
        self.assertEqual(paths.knowledge_db, self.root / "data" / "knowledge" / "knowledge.sqlite3")
        self.assertEqual(paths.runtime_db, self.root / "data" / "runtime" / "runtime.sqlite3")
        self.assertFalse((self.root / "data").exists())
        paths.ensure_runtime_directories()
        self.assertTrue(paths.backups_dir.is_dir())
        self.assertFalse(paths.knowledge_dir.exists())

    def test_06_stable_ids_and_health_snapshot(self) -> None:
        self.assertEqual(stable_id("job", "a", 1), stable_id("job", "a", 1))
        self.assertNotEqual(stable_id("job", "a", 1), stable_id("job", 1, "a"))
        health = HealthRegistry()
        self.assertEqual(health.snapshot().status, HealthStatus.UNKNOWN)
        health.set("storage", HealthStatus.HEALTHY, "sqlite ok")
        health.set("provider", HealthStatus.DEGRADED, "stale")
        self.assertEqual(health.snapshot().status, HealthStatus.DEGRADED)

    def test_07_logging_is_json_structured_and_redacts_secrets(self) -> None:
        logger = configure_logging(self.root / "logs", "INFO")
        logger.info("request failed api_key=abc123", extra={"event": "provider_error", "component": "data"})
        for handler in logger.handlers:
            handler.flush()
        entry = json.loads((self.root / "logs" / "invest-ai.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(entry["event"], "provider_error")
        self.assertIn("[REDACTED]", entry["message"])
        self.assertNotIn("abc123", json.dumps(entry))
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    def test_08_jobs_are_durable_idempotent_and_recoverable(self) -> None:
        store = RuntimeStore(self.root / "data" / "runtime" / "runtime.sqlite3")
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        first = store.enqueue("refresh", {"symbol": "ABC"}, "refresh:ABC", available_at=now)
        duplicate = store.enqueue("refresh", {"symbol": "ABC"}, "refresh:ABC", available_at=now)
        self.assertEqual(first, duplicate)
        with self.assertRaises(JobStateError):
            store.enqueue("refresh", {"symbol": "XYZ"}, "refresh:ABC", available_at=now)
        job = store.claim_next(now=now, lease_seconds=30)
        self.assertEqual(job.job_id, first)
        self.assertEqual(job.status, "RUNNING")
        self.assertIsNone(store.claim_next(now=now + timedelta(seconds=10)))
        self.assertEqual(store.recover_expired(now=now + timedelta(seconds=31)), 1)
        resumed = store.claim_next(now=now + timedelta(seconds=31), lease_seconds=30)
        self.assertEqual(resumed.attempts, 2)
        db = sqlite3.connect(store.path)
        try:
            attempt = db.execute("SELECT outcome FROM job_attempts WHERE job_id=? AND attempt_number=1", (first,)).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(attempt, "LEASE_EXPIRED")
        store.complete(first, now=now + timedelta(seconds=32))
        self.assertEqual(store.get_job(first).status, "COMPLETED")
        event_id = store.append_event("RuntimeStarted", {"mode": "RESEARCH"}, occurred_at=now)
        self.assertEqual(store.events()[0]["event_id"], event_id)

    def test_09_job_state_and_retry_policy_are_explicit(self) -> None:
        store = RuntimeStore(self.root / "runtime.sqlite3")
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        job_id = store.enqueue("probe", {}, "probe:1", max_attempts=1, available_at=now)
        with self.assertRaises(JobStateError):
            store.complete(job_id)
        store.claim_next(now=now)
        self.assertEqual(store.fail(job_id, "provider unavailable", retryable=True, now=now), "FAILED")
        self.assertEqual(store.get_job(job_id).status, "FAILED")
        secret_job = store.enqueue("probe", {}, "probe:secret", max_attempts=1, available_at=now)
        store.claim_next(now=now)
        store.fail(secret_job, "api_key=should-not-persist", retryable=False, now=now)
        db = sqlite3.connect(store.path)
        try:
            error = db.execute("SELECT last_error FROM jobs WHERE job_id=?", (secret_job,)).fetchone()[0]
        finally:
            db.close()
        self.assertNotIn("should-not-persist", error)
        self.assertIn("[REDACTED]", error)

    @unittest.skipUnless(sys.platform == "win32", "Windows named mutex")
    def test_10_instance_guard_blocks_second_runtime(self) -> None:
        guard = InstanceGuard(self.root).acquire()
        try:
            with self.assertRaises(AlreadyRunningError):
                InstanceGuard(self.root).acquire()
        finally:
            guard.release()
        with InstanceGuard(self.root):
            pass

    def _make_knowledge_db(self, path: Path) -> None:
        db = sqlite3.connect(path)
        try:
            db.executescript("""
            CREATE TABLE schema_info(singleton INTEGER PRIMARY KEY CHECK(singleton=1),schema_version INTEGER NOT NULL);
            INSERT INTO schema_info VALUES(1,7);
            CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, heading, text);
            INSERT INTO chunks_fts VALUES('chunk-1','Architecture','point in time evidence survives restore');
            """)
            db.commit()
        finally:
            db.close()

    def test_11_backup_manifest_hash_schema_and_restore_fts5(self) -> None:
        source = self.root / "knowledge.sqlite3"
        self._make_knowledge_db(source)
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        backup_dir = backup_knowledge_database(source, self.root / "backups", created_at=datetime(2026, 10, 7, 12, tzinfo=timezone.utc))
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 7)
        self.assertEqual(manifest["fts5_rows"], 1)
        self.assertEqual(manifest["database_sha256"], hashlib.sha256((backup_dir / "knowledge.sqlite3").read_bytes()).hexdigest())
        self.assertTrue(manifest["created_at_utc"].endswith("Z"))
        self.assertEqual({item.name for item in backup_dir.iterdir()}, {"knowledge.sqlite3", "manifest.json"})
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
        restored = self.root / "restored.sqlite3"
        result = restore_knowledge_backup(backup_dir, restored)
        self.assertEqual(result["integrity_check"], "ok")
        self.assertEqual(result["schema_version"], 7)
        self.assertFalse(Path(str(restored) + "-wal").exists())
        self.assertFalse(Path(str(restored) + "-shm").exists())
        db = sqlite3.connect(restored)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'evidence'").fetchone()[0], 1)
        finally:
            db.close()

    def test_12_backup_refuses_tampered_or_existing_restore_target(self) -> None:
        source = self.root / "knowledge.sqlite3"
        self._make_knowledge_db(source)
        backup_dir = backup_knowledge_database(source, self.root / "backups")
        with (backup_dir / "knowledge.sqlite3").open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaises(Exception):
            restore_knowledge_backup(backup_dir, self.root / "restored.sqlite3")
        self._make_knowledge_db(self.root / "existing.sqlite3")
        intact_backup = backup_knowledge_database(self.root / "existing.sqlite3", self.root / "other-backups")
        with self.assertRaises(Exception):
            restore_knowledge_backup(intact_backup, self.root / "existing.sqlite3")

    @unittest.skipUnless(sys.platform == "win32", "Windows runtime guard")
    def test_13_bootstrap_recovers_expired_jobs_without_starting_worker(self) -> None:
        paths = RuntimePaths.from_root(self.root)
        store = RuntimeStore(paths.runtime_db)
        before = utc_now()
        job_id = store.enqueue("restart-check", {}, "restart-check:1", available_at=before - timedelta(seconds=10))
        store.claim_next(now=before - timedelta(seconds=5), lease_seconds=1)
        with bootstrap_runtime(self.root) as runtime:
            self.assertEqual(runtime.config.mode, RuntimeMode.RESEARCH)
            self.assertEqual(runtime.recovered_jobs, 1)
            self.assertEqual(runtime.store.get_job(job_id).status, "PENDING")
            self.assertEqual(runtime.health.snapshot().status, HealthStatus.HEALTHY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
