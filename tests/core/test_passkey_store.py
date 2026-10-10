from __future__ import annotations

import sqlite3
import hashlib
import importlib.util
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from invest_ai.core.orchestration import OrchestrationStore


class PasskeySchemaMigrationTests(unittest.TestCase):
    def test_atomic_approval_rolls_back_challenge_and_accepts_zero_counter(self):
        from invest_ai.core.clock import utc_now
        from invest_ai.core.passkey_store import (
            PasskeyCredential, PasskeyStore, PasskeyStoreError, PolicyApprovalReceipt,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            orchestration = OrchestrationStore(path)
            orchestration.clear_kill_switch(actor="test", reason="approval fixture")
            control_epoch = orchestration.control_state().control_epoch
            policy_json = '{"version":"candidate-v1"}'
            policy_digest = hashlib.sha256(policy_json.encode("utf-8")).hexdigest()
            db = sqlite3.connect(path)
            try:
                db.execute(
                    "INSERT INTO risk_policy_revisions VALUES(?,?,?,?,?,?,?,?)",
                    ("revision-atomic", "candidate-v1", policy_json, policy_digest, "PENDING",
                     "2026-10-10T00:00:00.000000Z", "2026-10-10T00:00:00.000000Z", "test"),
                )
                db.commit()
            finally:
                db.close()

            store = PasskeyStore(orchestration)
            now = utc_now()
            credential = PasskeyCredential(
                credential_id="credential-atomic", public_key=b"test-public-key",
                user_handle="local-owner", sign_count=0, transports=("internal",), created_at=now,
            )
            store.save_credential(credential)
            readiness_digest = "b" * 64
            challenge = store.create_challenge(
                purpose="approve-risk-policy",
                context={"revision_id": "revision-atomic", "policy_digest": policy_digest,
                         "readiness_digest": readiness_digest, "control_epoch": control_epoch},
                expires_at=now + timedelta(minutes=2),
            )
            now = utc_now()
            receipt = PolicyApprovalReceipt.issue(
                revision_id="revision-atomic", policy_digest=policy_digest,
                readiness_digest=readiness_digest, control_epoch=control_epoch,
                credential_id=credential.credential_id, challenge_id=challenge.challenge_id,
                assertion_digest="c" * 64, recorded_at=now,
            )
            db = sqlite3.connect(path)
            try:
                db.execute(
                    "CREATE TRIGGER reject_receipt BEFORE INSERT ON risk_policy_approval_receipts "
                    "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
                )
                db.commit()
            finally:
                db.close()

            with self.assertRaises(sqlite3.IntegrityError):
                store.complete_approval(receipt=receipt, sign_count=0, now=now)
            db = sqlite3.connect(path)
            try:
                self.assertIsNone(db.execute(
                    "SELECT consumed_at FROM webauthn_challenges WHERE challenge_id=?",
                    (challenge.challenge_id,),
                ).fetchone()[0])
                self.assertEqual(db.execute(
                    "SELECT sign_count FROM passkey_credentials WHERE credential_id=?",
                    (credential.credential_id,),
                ).fetchone()[0], 0)
                db.execute("DROP TRIGGER reject_receipt")
                db.commit()
            finally:
                db.close()

            # Authenticators with a permanently zero counter are permitted by WebAuthn.
            store.complete_approval(receipt=receipt, sign_count=0, now=now)
            self.assertEqual(store.approval_receipt("revision-atomic"), receipt)

    def test_atomic_registration_rolls_back_challenge_when_credential_insert_fails(self):
        from invest_ai.core.clock import utc_now
        from invest_ai.core.passkey_store import PasskeyCredential, PasskeyStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            store = PasskeyStore(OrchestrationStore(path))
            now = utc_now()
            challenge = store.create_challenge(
                purpose="register-passkey", context={"user_handle": "local-owner"},
                expires_at=now + timedelta(minutes=2),
            )
            now = utc_now()
            credential = PasskeyCredential(
                credential_id="credential-registration", public_key=b"test-public-key",
                user_handle="local-owner", sign_count=0, transports=("internal",), created_at=now,
            )
            db = sqlite3.connect(path)
            try:
                db.execute(
                    "CREATE TRIGGER reject_credential BEFORE INSERT ON passkey_credentials "
                    "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
                )
                db.commit()
            finally:
                db.close()
            with self.assertRaises(sqlite3.IntegrityError):
                store.complete_registration(challenge_id=challenge.challenge_id, now=now,
                                            credential=credential)
            db = sqlite3.connect(path)
            try:
                self.assertIsNone(db.execute(
                    "SELECT consumed_at FROM webauthn_challenges WHERE challenge_id=?",
                    (challenge.challenge_id,),
                ).fetchone()[0])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM passkey_credentials").fetchone()[0], 0)
            finally:
                db.close()

    def test_v3_database_gets_additive_passkey_tables_and_preserves_control_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            original = OrchestrationStore(path)
            original.clear_kill_switch(actor="local-owner", reason="migration fixture")
            original.activate_kill_switch(actor="local-owner", reason="migration fixture halted")
            expected_control = original.control_state()
            policy_json = '{"legacy":"candidate"}'
            policy_digest = hashlib.sha256(policy_json.encode("utf-8")).hexdigest()
            db = sqlite3.connect(path)
            try:
                db.execute(
                    "INSERT INTO risk_policy_revisions VALUES(?,?,?,?,?,?,?,?)",
                    ("legacy-candidate", "legacy-v1", policy_json, policy_digest, "PENDING",
                     "2026-10-10T00:00:00.000000Z", "2026-10-10T00:00:00.000000Z", "v3 fixture"),
                )
                db.commit()
            finally:
                db.close()

            # Recreate the exact v3 boundary: the legacy database has no passkey objects.
            db = sqlite3.connect(path)
            try:
                for table in (
                    "risk_policy_approval_receipts",
                    "webauthn_challenges",
                    "passkey_credentials",
                ):
                    db.execute(f"DROP TABLE IF EXISTS {table}")
                db.execute("UPDATE orchestration_meta SET version=3")
                db.commit()
            finally:
                db.close()

            migrated = OrchestrationStore(path)
            self.assertEqual(migrated.control_state(), expected_control)
            self.assertTrue(migrated.kill_switch_active())

            db = sqlite3.connect(path)
            try:
                self.assertEqual(db.execute("SELECT version FROM orchestration_meta").fetchone()[0], 4)
                objects = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
                self.assertTrue({
                    "passkey_credentials",
                    "webauthn_challenges",
                    "risk_policy_approval_receipts",
                    "risk_policy_approval_receipts_no_update",
                    "risk_policy_approval_receipts_no_delete",
                    "webauthn_challenges_one_use",
                    "passkey_credentials_protect_identity",
                    "risk_policy_approval_receipts_require_verified_context",
                }.issubset(objects))
                self.assertIsNone(db.execute(
                    "SELECT active_revision_id FROM risk_policy_control WHERE singleton=1"
                ).fetchone()[0])
                self.assertEqual(db.execute(
                    "SELECT revision_id,policy_digest FROM risk_policy_revisions"
                ).fetchone(), ("legacy-candidate", policy_digest))
            finally:
                db.close()

    def test_passkey_store_module_exposes_durable_store_and_contracts(self):
        spec = importlib.util.find_spec("invest_ai.core.passkey_store")
        self.assertIsNotNone(spec, "durable passkey storage module is missing")

    def test_challenge_is_purpose_bound_expiring_and_consumed_once(self):
        spec = importlib.util.find_spec("invest_ai.core.passkey_store")
        self.assertIsNotNone(spec, "durable passkey storage module is missing")
        from invest_ai.core.clock import utc_now
        from invest_ai.core.passkey_store import PasskeyStore, PasskeyStoreError

        with tempfile.TemporaryDirectory() as directory:
            store = PasskeyStore(OrchestrationStore(Path(directory) / "runtime.sqlite3"))
            now = utc_now()
            created = store.create_challenge(
                purpose="approve-risk-policy",
                context={"revision_id": "revision-1", "policy_digest": "a" * 64,
                         "readiness_digest": "b" * 64, "control_epoch": 2},
                expires_at=now + timedelta(minutes=2),
            )
            consume_at = utc_now()
            consumed = store.consume_challenge(
                challenge_id=created.challenge_id, purpose="approve-risk-policy", now=consume_at,
            )
            self.assertEqual(consumed, created)
            with self.assertRaises(PasskeyStoreError):
                store.consume_challenge(
                    challenge_id=created.challenge_id, purpose="approve-risk-policy", now=now,
                )
            with self.assertRaises(PasskeyStoreError):
                store.create_challenge(
                    purpose="register-passkey", context={}, expires_at=now + timedelta(minutes=2, seconds=1),
                )

    def test_expired_or_wrong_purpose_challenge_is_rejected(self):
        spec = importlib.util.find_spec("invest_ai.core.passkey_store")
        self.assertIsNotNone(spec, "durable passkey storage module is missing")
        from invest_ai.core.clock import utc_now
        from invest_ai.core.passkey_store import PasskeyStore, PasskeyStoreError

        with tempfile.TemporaryDirectory() as directory:
            store = PasskeyStore(OrchestrationStore(Path(directory) / "runtime.sqlite3"))
            now = utc_now()
            challenge = store.create_challenge(
                purpose="approve-risk-policy", context={}, expires_at=now + timedelta(seconds=30),
            )
            with self.assertRaises(PasskeyStoreError):
                store.consume_challenge(
                    challenge_id=challenge.challenge_id, purpose="register-passkey", now=now,
                )
            with self.assertRaises(PasskeyStoreError):
                store.consume_challenge(
                    challenge_id=challenge.challenge_id, purpose="approve-risk-policy",
                    now=now + timedelta(seconds=31),
                )

    def test_v4_schema_corruption_fails_closed_instead_of_recreating_missing_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            OrchestrationStore(path)
            db = sqlite3.connect(path)
            try:
                db.execute("DROP TABLE passkey_credentials")
                db.commit()
            finally:
                db.close()
            with self.assertRaisesRegex(RuntimeError, "schema is incomplete"):
                OrchestrationStore(path)
            db = sqlite3.connect(path)
            try:
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='passkey_credentials'"
                ).fetchone())
            finally:
                db.close()

    def test_concurrent_challenge_consumption_has_exactly_one_winner(self):
        spec = importlib.util.find_spec("invest_ai.core.passkey_store")
        self.assertIsNotNone(spec, "durable passkey storage module is missing")
        from invest_ai.core.clock import utc_now
        from invest_ai.core.passkey_store import PasskeyStore, PasskeyStoreError

        with tempfile.TemporaryDirectory() as directory:
            store = PasskeyStore(OrchestrationStore(Path(directory) / "runtime.sqlite3"))
            now = utc_now()
            challenge = store.create_challenge(
                purpose="approve-risk-policy", context={}, expires_at=now + timedelta(minutes=1),
            )
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def consume():
                barrier.wait()
                try:
                    store.consume_challenge(
                        challenge_id=challenge.challenge_id, purpose="approve-risk-policy", now=utc_now(),
                    )
                    outcomes.append("consumed")
                except PasskeyStoreError:
                    outcomes.append("rejected")

            workers = [threading.Thread(target=consume) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertCountEqual(outcomes, ["consumed", "rejected"])

    def test_credential_and_approval_receipt_are_durable_and_append_only(self):
        spec = importlib.util.find_spec("invest_ai.core.passkey_store")
        self.assertIsNotNone(spec, "durable passkey storage module is missing")
        from invest_ai.core.clock import utc_now
        from invest_ai.core.passkey_store import (
            PasskeyCredential, PasskeyStore, PasskeyStoreError, PolicyApprovalReceipt,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            orchestration = OrchestrationStore(path)
            policy_json = '{"version":"candidate-v1"}'
            policy_digest = hashlib.sha256(policy_json.encode("utf-8")).hexdigest()
            db = sqlite3.connect(path)
            try:
                db.execute(
                    "INSERT INTO risk_policy_revisions VALUES(?,?,?,?,?,?,?,?)",
                    ("revision-1", "candidate-v1", policy_json, policy_digest, "PENDING",
                     "2026-10-10T00:00:00.000000Z", "2026-10-10T00:00:00.000000Z", "test candidate"),
                )
                db.commit()
            finally:
                db.close()

            store = PasskeyStore(orchestration)
            now = utc_now()
            credential = PasskeyCredential(
                credential_id="cred-1", public_key=b"public-key-test",
                user_handle="local-owner", sign_count=0, transports=("internal",), created_at=now,
            )
            store.save_credential(credential)
            store.update_credential_use("cred-1", sign_count=0, used_at=now + timedelta(seconds=1))
            store.update_credential_use("cred-1", sign_count=0, used_at=now + timedelta(seconds=2))
            store.update_credential_use("cred-1", sign_count=1, used_at=now + timedelta(seconds=3))
            with self.assertRaises(PasskeyStoreError):
                store.update_credential_use("cred-1", sign_count=0, used_at=now + timedelta(seconds=4))
            challenge = store.create_challenge(
                purpose="approve-risk-policy",
                context={"revision_id": "revision-1", "policy_digest": policy_digest,
                         "readiness_digest": "b" * 64, "control_epoch": 0},
                expires_at=now + timedelta(minutes=2),
            )
            store.consume_challenge(
                challenge_id=challenge.challenge_id, purpose="approve-risk-policy", now=utc_now(),
            )
            approval_time = utc_now()
            receipt = PolicyApprovalReceipt.issue(
                revision_id="revision-1", policy_digest=policy_digest,
                readiness_digest="b" * 64, control_epoch=0, credential_id="cred-1",
                challenge_id=challenge.challenge_id, assertion_digest="c" * 64, recorded_at=approval_time,
            )
            store.append_approval(receipt)
            reopened = PasskeyStore(OrchestrationStore(path))
            reopened.verify_integrity()
            restored_credential = reopened.credential("cred-1")
            self.assertEqual(restored_credential.credential_id, credential.credential_id)
            self.assertEqual(restored_credential.public_key, credential.public_key)
            self.assertEqual(restored_credential.sign_count, 1)
            self.assertEqual(reopened.approval_receipt("revision-1"), receipt)
            with self.assertRaises(PasskeyStoreError):
                reopened.update_credential_use("cred-1", sign_count=0, used_at=now + timedelta(seconds=5))

            db = sqlite3.connect(path)
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE risk_policy_approval_receipts SET receipt_digest=?", ("0" * 64,))
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("DELETE FROM risk_policy_approval_receipts")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE webauthn_challenges SET purpose='register-passkey' WHERE challenge_id=?",
                               (challenge.challenge_id,))
            finally:
                db.close()

            db = sqlite3.connect(path)
            try:
                db.execute("DROP TRIGGER risk_policy_approval_receipts_no_update")
                db.execute("UPDATE risk_policy_approval_receipts SET receipt_digest=?", ("0" * 64,))
                db.commit()
            finally:
                db.close()
            with self.assertRaisesRegex(PasskeyStoreError, "receipt digest"):
                PasskeyStore(orchestration)


if __name__ == "__main__":
    unittest.main()
