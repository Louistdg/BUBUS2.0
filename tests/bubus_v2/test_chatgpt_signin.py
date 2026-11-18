"""« Continuer avec ChatGPT » (version PC) avec un faux serveur OpenAI."""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from bubus import chatgpt_signin as cs
from bubus.assistant_chat import ChatConfig, ChatService, ChatUnavailable


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeProtector:
    available = True

    def protect(self, data):
        return b"enc:" + data[::-1]

    def unprotect(self, data):
        assert data.startswith(b"enc:")
        return data[4:][::-1]


class FakeOpenAI:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.key.public_key().public_numbers()
        self.jwks = {"keys": [{"kty": "RSA", "kid": "k1", "n": b64(numbers.n.to_bytes(256, "big")),
                               "e": b64(numbers.e.to_bytes(3, "big"))}]}
        self.nonce = None
        self.calls = []
        self.scope = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
        self.refresh_status = 200
        self.stream_events = [{"type": "response.output_text.delta", "delta": "Bonjour "},
                              {"type": "response.output_text.delta", "delta": "Louis"},
                              {"type": "response.completed"}]
        self.stream_status = 200

    def id_token(self, client_id, nonce, exp=10_000):
        header = b64(json.dumps({"alg": "RS256", "kid": "k1"}).encode())
        payload = b64(json.dumps({"iss": cs.ISSUER, "aud": client_id, "sub": "user-1", "email": "l@example.com",
                                  "nonce": nonce, "exp": exp}).encode())
        signature = self.key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{b64(signature)}"

    def http(self, method, url, *, form=None, headers=None, timeout=30):
        self.calls.append((method, url, form, headers))
        if url == cs.TOKEN_URL and form["grant_type"] == "authorization_code":
            return 200, {"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600, "scope": self.scope,
                         "id_token": self.id_token(form["client_id"], self.nonce)}
        if url == cs.TOKEN_URL and form["grant_type"] == "refresh_token":
            if self.refresh_status != 200:
                return self.refresh_status, {"error": "refresh_token_expired"}
            return 200, {"access_token": "acc-2", "refresh_token": "ref-2", "expires_in": 3600}
        if url == cs.MODELS_URL:
            return 200, {"models": [{"slug": "hidden-model", "visibility": "hide"},
                                    {"slug": "gpt-test", "display_name": "GPT Test", "visibility": "list"}]}
        if url == cs.DISCOVERY_URL:
            return 200, {"revocation_endpoint": "https://auth.openai.com/revoke"}
        if url == "https://auth.openai.com/revoke":
            return 200, {}
        raise AssertionError(url)

    def stream(self, token, body, timeout=120):
        self.calls.append(("STREAM", token, body))
        return self.stream_status, self.stream_events


class ChatGPTSignInTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1_000.0
        self.openai = FakeOpenAI()
        self.signin = cs.ChatGPTSignIn(Path(self.tmp.name), protector=FakeProtector(), http_json=self.openai.http,
                                       stream=self.openai.stream, jwks_loader=lambda: self.openai.jwks,
                                       clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self, client_id="oaiapp_123"):
        url = self.signin.start(8790)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.openai.nonce = query["nonce"]
        return query, self.signin.finish({"code": "c1", "state": query["state"], "client_id": client_id})

    def test_first_sign_in_uses_dynamic_registration_and_loopback(self):
        query, state = self.connect()
        self.assertEqual(query["client_id"], "dynamic_agent_client")
        self.assertEqual(query["agent_name_hint"], "BUBUS")
        self.assertEqual(query["redirect_uri"], "http://127.0.0.1:8790/auth/callback")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertIn("chatgpt.tokens.use.direct", query["scope"])
        self.assertTrue(query["ext_agent_host_id"].startswith("urn:uuid:"))
        exchange = next(c for c in self.openai.calls if c[2] and c[2].get("grant_type") == "authorization_code")
        self.assertEqual(exchange[2]["client_id"], "oaiapp_123")
        self.assertEqual(exchange[2]["resource"], cs.RESOURCE)
        self.assertTrue(state["signed_in"] and state["plan_enabled"])
        self.assertEqual(state["model"], "gpt-test")
        self.assertNotIn("acc-1", json.dumps(state))
        raw = (Path(self.tmp.name) / "chatgpt-session.bin").read_bytes()
        self.assertNotIn(b"acc-1", raw)
        second, _ = self.connect()
        self.assertEqual(second["client_id"], "oaiapp_123")
        self.assertNotIn("agent_name_hint", second)

    def test_bad_callbacks_are_refused(self):
        with self.assertRaises(cs.ChatGPTSignInError):
            self.signin.finish({"code": "c", "state": "inconnu"})
        url = self.signin.start(8790)
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
        with self.assertRaises(cs.ChatGPTSignInError):
            self.signin.finish({"code": "c", "state": state, "client_id": "dynamic_agent_client"})
        url = self.signin.start(8790)
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
        self.openai.nonce = "autre-nonce"
        with self.assertRaises(cs.ChatGPTSignInError):
            self.signin.finish({"code": "c", "state": state, "client_id": "oaiapp_123"})
        self.assertFalse(self.signin.state()["signed_in"])

    def test_tampered_id_token_is_refused(self):
        token = self.openai.id_token("oaiapp_123", "n")
        header, payload, signature = token.split(".")
        forged = b64(json.dumps({"iss": cs.ISSUER, "aud": "oaiapp_123", "sub": "x", "nonce": "n",
                                 "exp": 10_000}).encode())
        with self.assertRaises(cs.ChatGPTSignInError):
            cs.verify_id_token(f"{header}.{forged}.{signature}", client_id="oaiapp_123", nonce="n",
                               jwks=self.openai.jwks, now=1000)
        self.assertEqual(cs.verify_id_token(token, client_id="oaiapp_123", nonce="n", jwks=self.openai.jwks,
                                            now=1000)["sub"], "user-1")

    def test_missing_plan_scope_disables_usage(self):
        self.openai.scope = "openid profile email"
        _, state = self.connect()
        self.assertTrue(state["signed_in"])
        self.assertFalse(state["plan_enabled"])
        self.assertFalse(self.signin.ready)

    def test_completion_streams_text_and_refreshes_expired_token(self):
        self.connect()
        self.assertEqual(self.signin.complete(system="S", messages=[{"role": "user", "content": "Salut"}],
                                              snapshot="relevé"), "Bonjour Louis")
        body = self.openai.calls[-1][2]
        self.assertEqual((body["store"], body["stream"], body["model"]), (False, True, "gpt-test"))
        self.assertEqual(body["input"][0]["role"], "developer")
        self.assertIn("relevé", body["input"][-1]["content"])
        self.now += 4000
        self.signin.complete(system="S", messages=[{"role": "user", "content": "Encore"}], snapshot="r")
        self.assertEqual(self.openai.calls[-1][1], "acc-2")

    def test_expired_refresh_forgets_the_session(self):
        self.connect()
        self.now += 4000
        self.openai.refresh_status = 400
        with self.assertRaises(cs.ChatGPTSignInError) as error:
            self.signin.complete(system="S", messages=[{"role": "user", "content": "x"}], snapshot="r")
        self.assertEqual(error.exception.status, 401)
        self.assertFalse(self.signin.state()["signed_in"])

    def test_usage_limit_is_explained(self):
        self.connect()
        self.openai.stream_events = [{"type": "response.failed", "response": {"error": {
            "code": "subscription_sharing_usage_limit_exceeded"}}}]
        with self.assertRaises(cs.ChatGPTSignInError) as error:
            self.signin.complete(system="S", messages=[{"role": "user", "content": "x"}], snapshot="r")
        self.assertEqual(error.exception.status, 429)
        self.assertIn("chatgpt.com/settings/usage", str(error.exception))

    def test_disconnect_revokes_and_forgets(self):
        self.connect()
        self.signin.disconnect()
        self.assertFalse(self.signin.state()["signed_in"])
        revoke = [c for c in self.openai.calls if c[1] == "https://auth.openai.com/revoke"]
        self.assertEqual(revoke[0][2]["token"], "ref-1")

    def test_chat_service_prefers_the_subscription(self):
        self.connect()
        service = ChatService(config_loader=lambda: ChatConfig(None, None, None, 5), chatgpt=self.signin)
        state = service.state()
        self.assertTrue(state["ready"])
        self.assertEqual(state["provider"], "ChatGPT (ton abonnement)")
        result = service.reply([{"role": "user", "content": "Salut"}], "relevé")
        self.assertEqual(result["reply"], "Bonjour Louis")
        self.openai.stream_status = 429
        self.openai.stream_events = []
        with self.assertRaises(ChatUnavailable) as error:
            service.reply([{"role": "user", "content": "Salut"}], "relevé")
        self.assertEqual(error.exception.status, 429)

    def test_explicit_api_provider_wins_over_subscription(self):
        self.connect()
        calls = []
        service = ChatService(config_loader=lambda: ChatConfig("claude", "m", "k", 5), chatgpt=self.signin,
                              claude_call=lambda **kw: calls.append(kw) or "claude")
        with patch.dict("os.environ", {"BUBUS_AI_PROVIDER": "claude"}):
            self.assertEqual(service.reply([{"role": "user", "content": "x"}], "r")["reply"], "claude")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
