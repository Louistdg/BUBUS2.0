from __future__ import annotations

import tempfile
import unittest
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from invest_ai.core.orchestration import OrchestrationStore
from invest_ai.core.passkey_store import PasskeyCredential, PasskeyStore
from invest_ai.core.passkey_approval import (
    PasskeyApprovalService, PasskeyApprovalError, _credential_descriptor,
)
from invest_ai.core.risk_policy import RiskPolicyRegistry, RiskPolicyUnavailable
from invest_ai.risk import AssetType, RiskPolicy, Side


class PasskeyApprovalContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = OrchestrationStore(Path(self.temp.name) / "runtime.sqlite3")
        self.store.clear_kill_switch(actor="test", reason="approval fixture")
        self.passkeys = PasskeyStore(self.store)
        self.registry = RiskPolicyRegistry(self.store)
        self.service = PasskeyApprovalService(
            self.passkeys, self.registry,
            readiness_provider=lambda: {"status": "BLOCKED", "armed": False, "orders_enabled": False},
            rp_id="localhost", origin="http://localhost:8791",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _candidate_and_credential(
        self, *, credential_id="Zml4dHVyZS1jcmVkZW50aWFs", public_key=b"mock-public-key",
    ):
        now = datetime.now(timezone.utc)
        policy = RiskPolicy(
            version="candidate-v1", allowed_instruments=frozenset({"BTC_USD"}),
            allowed_sides=frozenset({Side.BUY}), allowed_asset_types=frozenset({AssetType.CRYPTO}),
            max_order_notional=Decimal("10"), max_position_notional=Decimal("10"),
            max_concentration_ratio=Decimal("0.5"), max_sector_concentration_ratio=Decimal("0.5"),
            max_gross_exposure=Decimal("10"), min_cash_balance=Decimal("0"),
            max_loss_per_trade=Decimal("2"), max_daily_loss=Decimal("5"),
            max_drawdown_ratio=Decimal("0.2"), max_leverage=Decimal("1"),
            max_market_data_age_seconds=60, max_portfolio_age_seconds=60,
            max_orders_per_window=3, frequency_window_seconds=60,
            fee_rate=Decimal("0.001"), slippage_buffer_rate=Decimal("0.002"),
        )
        revision = self.registry.register_candidate(
            policy, effective_at=now, recorded_at=now, reason="local passkey test",
        )
        credential = PasskeyCredential(
            credential_id=credential_id, public_key=public_key,
            user_handle=bytes_to_base64url(b"INVEST-AI-LOCAL-OWNER-V1"), sign_count=0,
            transports=("internal",), created_at=now,
        )
        self.passkeys.save_credential(credential)
        return revision, credential

    @staticmethod
    def _assertion(options, credential):
        client_data = {
            "type": "webauthn.get", "challenge": options.public_key["challenge"],
            "origin": "http://localhost:8791", "crossOrigin": False,
        }
        return {
            "id": credential.credential_id, "rawId": credential.credential_id,
            "type": "public-key", "authenticatorAttachment": "platform",
            "response": {
                "clientDataJSON": bytes_to_base64url(json.dumps(client_data).encode("utf-8")),
                "authenticatorData": "AA", "signature": "AA",
                "userHandle": credential.user_handle,
            },
        }

    def test_registration_options_require_user_verification_and_resident_key(self):
        options = self.service.begin_registration(now=datetime.now(timezone.utc))
        self.assertEqual(options.public_key["authenticatorSelection"]["userVerification"], "required")
        self.assertEqual(options.public_key["authenticatorSelection"]["residentKey"], "required")
        self.assertEqual(options.public_key["rp"]["id"], "localhost")

    def test_real_py_webauthn_registration_fixture_is_saved_atomically(self):
        self.service = PasskeyApprovalService(
            self.passkeys, self.registry,
            readiness_provider=lambda: {"status": "BLOCKED", "armed": False, "orders_enabled": False},
            rp_id="localhost", origin="http://localhost:5000",
        )
        expected_challenge = "CeTWogmg0cchuiYuFrv8DXXdMZSIQRVZJOga_xayVVEcBj0Cw3y73yhD4FkGSe-RrP6hPJJAIm3LVien4hXELg"
        with patch("invest_ai.core.passkey_store.secrets.token_urlsafe", return_value=expected_challenge):
            options = self.service.begin_registration(now=datetime.now(timezone.utc))
        self.assertEqual(options.public_key["challenge"], expected_challenge)
        response = {
            "id": "ZoIKP1JQvKdrYj1bTUPJ2eTUsbLeFkv-X5xJQNr4k6s",
            "rawId": "ZoIKP1JQvKdrYj1bTUPJ2eTUsbLeFkv-X5xJQNr4k6s",
            "response": {
                "attestationObject": "o2NmbXRkbm9uZWdhdHRTdG10oGhhdXRoRGF0YVkBZ0mWDeWIDoxodDQXD2R2YFuP5K65ooYyx5lc87qDHZdjRQAAAAAAAAAAAAAAAAAAAAAAAAAAACBmggo_UlC8p2tiPVtNQ8nZ5NSxst4WS_5fnElA2viTq6QBAwM5AQAgWQEA31dtHqc70D_h7XHQ6V_nBs3Tscu91kBL7FOw56_VFiaKYRH6Z4KLr4J0S12hFJ_3fBxpKfxyMfK66ZMeAVbOl_wemY4S5Xs4yHSWy21Xm_dgWhLJjZ9R1tjfV49kDPHB_ssdvP7wo3_NmoUPYMgK-edgZ_ehttp_I6hUUCnVaTvn_m76b2j9yEPReSwl-wlGsabYG6INUhTuhSOqG-UpVVQdNJVV7GmIPHCA2cQpJBDZBohT4MBGme_feUgm4sgqVCWzKk6CzIKIz5AIVnspLbu05SulAVnSTB3NxTwCLNJR_9v9oSkvphiNbmQBVQH1tV_psyi9HM1Jtj9VJVKMeyFDAQAB",
                "clientDataJSON": "eyJ0eXBlIjoid2ViYXV0aG4uY3JlYXRlIiwiY2hhbGxlbmdlIjoiQ2VUV29nbWcwY2NodWlZdUZydjhEWFhkTVpTSVFSVlpKT2dhX3hheVZWRWNCajBDdzN5NzN5aEQ0RmtHU2UtUnJQNmhQSkpBSW0zTFZpZW40aFhFTGciLCJvcmlnaW4iOiJodHRwOi8vbG9jYWxob3N0OjUwMDAiLCJjcm9zc09yaWdpbiI6ZmFsc2V9",
                "transports": ["internal"],
            },
            "type": "public-key", "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }
        credential = self.service.finish_registration(response=response, now=datetime.now(timezone.utc))
        self.assertEqual(credential.credential_id, response["id"])
        self.assertEqual(self.passkeys.credential(credential.credential_id), credential)
        challenge = self.passkeys.challenge_by_value(expected_challenge, purpose="register-passkey")
        self.assertIsNotNone(challenge)
        self.assertIsNotNone(self.passkeys.credentials())

    def test_remote_origin_configuration_is_rejected(self):
        with self.assertRaises(PasskeyApprovalError):
            PasskeyApprovalService(
                self.passkeys, self.registry,
                readiness_provider=lambda: {"status": "BLOCKED", "armed": False, "orders_enabled": False},
                rp_id="example.com", origin="https://example.com",
            )
        with self.assertRaises(PasskeyApprovalError):
            PasskeyApprovalService(
                self.passkeys, self.registry,
                readiness_provider=lambda: {"status": "BLOCKED", "armed": False, "orders_enabled": False},
                rp_id="localhost", origin="http://127.0.0.1:8791",
            )

    def test_policy_approval_is_not_available_without_a_registered_passkey(self):
        with self.assertRaises(PasskeyApprovalError):
            self.service.begin_policy_approval(revision_id="missing", now=datetime.now(timezone.utc))

    def test_incomplete_policy_candidate_is_rejected(self):
        revision, _ = self._candidate_and_credential()
        _, policy = self.registry.candidate(revision.revision_id)
        incomplete = replace(policy, max_daily_loss=Decimal("0"))
        invalid_revision = self.registry.register_candidate(
            incomplete, effective_at=datetime.now(timezone.utc), reason="invalid test policy",
        )
        with self.assertRaises(PasskeyApprovalError):
            self.service.begin_policy_approval(
                revision_id=invalid_revision.revision_id, now=datetime.now(timezone.utc),
            )

    def test_allow_credentials_encodes_stored_id_and_ignores_unknown_transport_hint(self):
        credential = PasskeyCredential(
            credential_id=bytes_to_base64url(b"fixture-credential"), public_key=b"mock-public-key",
            user_handle=bytes_to_base64url(b"INVEST-AI-LOCAL-OWNER-V1"), sign_count=0,
            transports=("future-transport",), created_at=datetime.now(timezone.utc),
        )
        descriptor = _credential_descriptor(credential)
        self.assertEqual(bytes_to_base64url(descriptor.id), credential.credential_id)
        self.assertIsNone(descriptor.transports)

    def test_missing_server_verified_uv_is_rejected_without_consuming_challenge(self):
        revision, credential = self._candidate_and_credential()
        now = datetime.now(timezone.utc)
        options = self.service.begin_policy_approval(revision_id=revision.revision_id, now=now)
        response = self._assertion(options, credential)
        with patch("invest_ai.core.passkey_approval.verify_authentication_response",
                   return_value=SimpleNamespace(user_verified=False, new_sign_count=0)) as verify:
            with self.assertRaises(PasskeyApprovalError):
                self.service.finish_policy_approval(response=response, now=datetime.now(timezone.utc))
        self.assertIs(verify.call_args.kwargs["require_user_verification"], True)
        challenge = self.passkeys.challenge_by_value(options.public_key["challenge"],
                                                      purpose="approve-risk-policy")
        self.assertIsNotNone(challenge)
        self.assertIsNone(self.registry.approval_receipt(revision.revision_id))

    def test_assertion_replay_is_rejected_after_first_receipt(self):
        revision, credential = self._candidate_and_credential()
        now = datetime.now(timezone.utc)
        options = self.service.begin_policy_approval(revision_id=revision.revision_id, now=now)
        response = self._assertion(options, credential)
        with patch("invest_ai.core.passkey_approval.verify_authentication_response",
                   return_value=SimpleNamespace(user_verified=True, new_sign_count=0)):
            receipt = self.service.finish_policy_approval(response=response, now=datetime.now(timezone.utc))
            self.assertEqual(receipt.revision_id, revision.revision_id)
            with self.assertRaises(PasskeyApprovalError):
                self.service.finish_policy_approval(response=response, now=datetime.now(timezone.utc))
        self.assertEqual(self.registry.approval_receipt(revision.revision_id), receipt)

    def test_real_py_webauthn_fixture_issues_a_fully_bound_receipt(self):
        credential_id = "ZoIKP1JQvKdrYj1bTUPJ2eTUsbLeFkv-X5xJQNr4k6s"
        public_key = base64url_to_bytes(
            "pAEDAzkBACBZAQDfV20epzvQP-HtcdDpX-cGzdOxy73WQEvsU7Dnr9UWJophEfpngouvgnRLXaEUn_d8HGkp_HIx8rrpkx4BVs6X_B6ZjhLlezjIdJbLbVeb92BaEsmNn1HW2N9Xj2QM8cH-yx28_vCjf82ahQ9gyAr552Bn96G22n8jqFRQKdVpO-f-bvpvaP3IQ9F5LCX7CUaxptgbog1SFO6FI6ob5SlVVB00lVXsaYg8cIDZxCkkENkGiFPgwEaZ7995SCbiyCpUJbMqToLMgojPkAhWeyktu7TlK6UBWdJMHc3FPAIs0lH_2_2hKS-mGI1uZAFVAfW1X-mzKL0czUm2P1UlUox7IUMBAAE"
        )
        revision, credential = self._candidate_and_credential(
            credential_id=credential_id, public_key=public_key,
        )
        expected_challenge = "iPmAi1Pp1XL6oAgq3PWZtZPnZa1zFUDoGbaQ0_KvVG1lF2s3Rt_3o4uSzccy0tmcTIpTTT4BU1T-I4maavndjQ"
        self.service = PasskeyApprovalService(
            self.passkeys, self.registry,
            readiness_provider=lambda: {"status": "BLOCKED", "armed": False, "orders_enabled": False},
            rp_id="localhost", origin="http://localhost:5000",
        )
        now = datetime.now(timezone.utc)
        with patch("invest_ai.core.passkey_store.secrets.token_urlsafe", return_value=expected_challenge):
            options = self.service.begin_policy_approval(revision_id=revision.revision_id, now=now)
        self.assertEqual(options.public_key["challenge"], expected_challenge)
        response = {
            "id": credential_id, "rawId": credential_id, "type": "public-key",
            "authenticatorAttachment": "cross-platform", "clientExtensionResults": {},
            "response": {
                "authenticatorData": "SZYN5YgOjGh0NBcPZHZgW4_krrmihjLHmVzzuoMdl2MFAAAAAQ",
                "clientDataJSON": "eyJ0eXBlIjoid2ViYXV0aG4uZ2V0IiwiY2hhbGxlbmdlIjoiaVBtQWkxUHAxWEw2b0FncTNQV1p0WlBuWmExekZVRG9HYmFRMF9LdlZHMWxGMnMzUnRfM280dVN6Y2N5MHRtY1RJcFRUVDRCVTFULUk0bWFhdm5kalEiLCJvcmlnaW4iOiJodHRwOi8vbG9jYWxob3N0OjUwMDAiLCJjcm9zc09yaWdpbiI6ZmFsc2V9",
                "signature": "iOHKX3erU5_OYP_r_9HLZ-CexCE4bQRrxM8WmuoKTDdhAnZSeTP0sjECjvjfeS8MJzN1ArmvV0H0C3yy_FdRFfcpUPZzdZ7bBcmPh1XPdxRwY747OrIzcTLTFQUPdn1U-izCZtP_78VGw9pCpdMsv4CUzZdJbEcRtQuRS03qUjqDaovoJhOqEBmxJn9Wu8tBi_Qx7A33RbYjlfyLm_EDqimzDZhyietyop6XUcpKarKqVH0M6mMrM5zTjp8xf3W7odFCadXEJg-ERZqFM0-9Uup6kJNLbr6C5J4NDYmSm3HCSA6lp2iEiMPKU8Ii7QZ61kybXLxsX4w4Dm3fOLjmDw",
                "userHandle": credential.user_handle,
            },
        }
        receipt = self.service.finish_policy_approval(response=response, now=datetime.now(timezone.utc))
        self.assertEqual(receipt.revision_id, revision.revision_id)
        self.assertEqual(receipt.policy_digest, revision.policy_digest)
        self.assertEqual(receipt.control_epoch, self.store.control_state().control_epoch)
        self.assertEqual(receipt.credential_id, credential.credential_id)
        self.assertEqual(self.registry.approval_receipt(revision.revision_id), receipt)
        current, _ = self.registry.candidate(revision.revision_id)
        self.assertEqual(current.approval_status, "PENDING")
        with self.assertRaises(RiskPolicyUnavailable):
            self.registry.active_policy(now=datetime.now(timezone.utc))

    def test_unexpected_origin_is_rejected_before_library_verification(self):
        revision, credential = self._candidate_and_credential()
        now = datetime.now(timezone.utc)
        options = self.service.begin_policy_approval(revision_id=revision.revision_id, now=now)
        response = self._assertion(options, credential)
        client_data = json.loads(base64url_to_bytes(response["response"]["clientDataJSON"]))
        client_data["origin"] = "http://localhost.evil.example:8791"
        response["response"]["clientDataJSON"] = bytes_to_base64url(
            json.dumps(client_data).encode("utf-8"))
        with patch("invest_ai.core.passkey_approval.verify_authentication_response") as verify:
            with self.assertRaises(PasskeyApprovalError):
                self.service.finish_policy_approval(response=response, now=datetime.now(timezone.utc))
        verify.assert_not_called()

    def test_expired_approval_challenge_is_rejected(self):
        revision, credential = self._candidate_and_credential()
        now = datetime.now(timezone.utc)
        options = self.service.begin_policy_approval(revision_id=revision.revision_id, now=now)
        with patch("invest_ai.core.passkey_approval.verify_authentication_response") as verify:
            with self.assertRaises(PasskeyApprovalError):
                self.service.finish_policy_approval(
                    response=self._assertion(options, credential), now=now + timedelta(minutes=3),
                )
        verify.assert_not_called()

    def test_unexpected_challenge_and_wrong_ceremony_type_are_rejected(self):
        revision, credential = self._candidate_and_credential()
        now = datetime.now(timezone.utc)
        options = self.service.begin_policy_approval(revision_id=revision.revision_id, now=now)
        response = self._assertion(options, credential)
        client_data = json.loads(base64url_to_bytes(response["response"]["clientDataJSON"]))
        client_data["challenge"] = bytes_to_base64url(b"not-the-issued-challenge")
        response["response"]["clientDataJSON"] = bytes_to_base64url(
            json.dumps(client_data).encode("utf-8"))
        with patch("invest_ai.core.passkey_approval.verify_authentication_response") as verify:
            with self.assertRaises(PasskeyApprovalError):
                self.service.finish_policy_approval(response=response, now=datetime.now(timezone.utc))
            client_data["challenge"] = options.public_key["challenge"]
            client_data["type"] = "webauthn.create"
            response["response"]["clientDataJSON"] = bytes_to_base64url(
                json.dumps(client_data).encode("utf-8"))
            with self.assertRaises(PasskeyApprovalError):
                self.service.finish_policy_approval(response=response, now=datetime.now(timezone.utc))
        verify.assert_not_called()

    def test_control_epoch_change_and_halted_state_reject_approval(self):
        revision, credential = self._candidate_and_credential()
        now = datetime.now(timezone.utc)
        options = self.service.begin_policy_approval(revision_id=revision.revision_id, now=now)
        self.store.activate_kill_switch(actor="test", reason="epoch changed during approval")
        with patch("invest_ai.core.passkey_approval.verify_authentication_response") as verify:
            with self.assertRaises(PasskeyApprovalError):
                self.service.finish_policy_approval(
                    response=self._assertion(options, credential), now=datetime.now(timezone.utc),
                )
        verify.assert_not_called()

    def test_policy_digest_change_after_challenge_rejects_approval(self):
        revision, credential = self._candidate_and_credential()
        now = datetime.now(timezone.utc)
        options = self.service.begin_policy_approval(revision_id=revision.revision_id, now=now)
        with self.store._connection() as db:
            db.execute("DROP TRIGGER risk_policy_revisions_no_update")
            db.execute("UPDATE risk_policy_revisions SET policy_digest=? WHERE revision_id=?",
                       ("0" * 64, revision.revision_id))
        with patch("invest_ai.core.passkey_approval.verify_authentication_response") as verify:
            with self.assertRaises(PasskeyApprovalError):
                self.service.finish_policy_approval(
                    response=self._assertion(options, credential), now=datetime.now(timezone.utc),
                )
        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
