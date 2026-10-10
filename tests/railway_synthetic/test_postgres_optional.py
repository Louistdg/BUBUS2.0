"""PostgreSQL adapter checks. Live integration runs only on an explicit test DSN."""
from __future__ import annotations

import importlib.util
import os
import re
import threading
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlparse

from invest_ai.railway_synthetic.fixtures import make_first_snapshot
from invest_ai.railway_synthetic.postgres import MIGRATIONS, PostgresProjectionStore


TEST_DSN = os.environ.get("INVEST_AI_SYNTHETIC_POSTGRES_TEST_DSN", "")
PG_AVAILABLE = bool(TEST_DSN and importlib.util.find_spec("psycopg"))


class PostgresMigrationStaticTests(unittest.TestCase):
    def test_versioned_migration_is_synthetic_only_and_has_atomic_projection_keys(self):
        self.assertEqual([version for version, _ in MIGRATIONS], [1])
        sql = MIGRATIONS[0][1].read_text(encoding="utf-8").lower()
        for required in ("synthetic_schema_migrations", "synthetic_streams", "synthetic_snapshots", "primary key", "jsonb", "references synthetic_streams", "unique"):
            self.assertIn(required, sql)
        self.assertNotIn("coordination.sqlite3", sql)
        self.assertNotIn("invest_ai.coordination", sql)

    def test_postgres_connection_factory_requires_explicit_postgresql_dsn(self):
        from invest_ai.railway_synthetic.postgres import psycopg_connection_factory
        with self.assertRaises(ValueError):
            psycopg_connection_factory("")
        with self.assertRaises(ValueError):
            psycopg_connection_factory("sqlite:///coordination.sqlite3")

    def test_projection_package_has_no_core_database_import_or_path(self):
        package = Path(__file__).resolve().parents[2] / "src" / "invest_ai" / "railway_synthetic"
        for path in package.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(?m)^\s*(?:from|import)\s+invest_ai\.coordination\b", source), path.name)
            self.assertNotIn("data/coordination/coordination.sqlite3", source.lower(), path.name)


@unittest.skipUnless(PG_AVAILABLE, "requires psycopg and explicit disposable INVEST_AI_SYNTHETIC_POSTGRES_TEST_DSN")
class DisposablePostgresIntegrationTests(unittest.TestCase):
    """Never runs against an unspecified DB; database name must contain synthetic_test."""

    @classmethod
    def setUpClass(cls):
        parsed = urlparse(TEST_DSN)
        if "synthetic_test" not in parsed.path.casefold():
            raise RuntimeError("PostgreSQL integration DSN database name must contain synthetic_test")
        import psycopg

        cls.psycopg = psycopg
        cls.schema = "synthetic_test_" + uuid.uuid4().hex

        def raw_connect():
            return psycopg.connect(TEST_DSN)

        with raw_connect() as connection:
            connection.execute(f'CREATE SCHEMA "{cls.schema}"')
        cls.connect = staticmethod(lambda: cls._connect_schema(raw_connect))

    @classmethod
    def _connect_schema(cls, raw_connect):
        connection = raw_connect()
        connection.execute(f'SET search_path TO "{cls.schema}"')
        connection.commit()
        return connection

    @classmethod
    def tearDownClass(cls):
        with cls.psycopg.connect(TEST_DSN) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')

    def test_migration_idempotence_chain_concurrency_rollback_and_corruption(self):
        store = PostgresProjectionStore(self.connect)
        store.migrate()
        store.migrate()
        first = make_first_snapshot()
        fail_commit = {"enabled": True}

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                if fail_commit["enabled"]:
                    fail_commit["enabled"] = False
                    raise RuntimeError("injected PostgreSQL commit failure")
                self.connection.commit()

        failing_store = PostgresProjectionStore(lambda: ConnectionProxy(self.connect()))
        with self.assertRaises(RuntimeError):
            failing_store.ingest(first)
        metadata, snapshot = store.projection()
        self.assertEqual(metadata["sync_state"], "UNKNOWN")
        self.assertIsNone(snapshot)

        results: list[str] = []
        errors: list[BaseException] = []

        def ingest():
            try:
                results.append(store.ingest(first)["result"])
            except BaseException as error:
                errors.append(error)

        workers = [threading.Thread(target=ingest) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertFalse(errors)
        self.assertEqual(results.count("ACCEPTED"), 1)
        self.assertEqual(results.count("IDEMPOTENT"), 3)
        metadata, snapshot = store.projection()
        self.assertEqual(metadata["sync_state"], "UNKNOWN")
        self.assertEqual(store.handshake(first["source_instance_id"], 1, first["snapshot_sha256"]), "MATCH")
        metadata, snapshot = store.projection()
        self.assertEqual(metadata["sync_state"], "FRESH")
        self.assertEqual(metadata["snapshot_sequence"], 1)
        self.assertEqual(snapshot, first)

        with self.connect() as connection:
            connection.execute("UPDATE synthetic_snapshots SET payload=jsonb_set(payload,'{payload,demo_summary,simulated_state}','\"SIMULATED_RED\"'::jsonb) WHERE sequence=1")
        metadata, snapshot = store.projection()
        self.assertEqual(metadata["sync_state"], "UNKNOWN")
        self.assertIsNotNone(snapshot)
        with self.connect() as connection:
            connection.execute("DELETE FROM synthetic_snapshots")
            connection.execute("DELETE FROM synthetic_streams")
            connection.execute("DELETE FROM synthetic_schema_migrations")
        store.migrate()
        self.assertEqual(store.ingest(first)["result"], "ACCEPTED")
        self.assertEqual(store.handshake(first["source_instance_id"], 1, first["snapshot_sha256"]), "MATCH")
        restored_metadata, restored_snapshot = store.projection()
        self.assertEqual(restored_metadata["sync_state"], "FRESH")
        self.assertEqual(restored_snapshot["snapshot_sha256"], first["snapshot_sha256"])


if __name__ == "__main__":
    unittest.main()
