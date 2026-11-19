"""OpenRouter : clé par variable (en ligne) ou saisie dans l'appli (PC, chiffrée par Windows)."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from bubus import assistant_chat
from bubus.ai_keys import AIKeyError, OpenRouterKeyStore
from bubus.assistant_chat import ChatConfig, ChatService, ChatUnavailable, load_config
from tests.bubus_v2.test_chatgpt_signin import FakeProtector
from tests.bubus_v2.test_cloud_app import RecordingCall, _ServerCase

KEY = "sk-or-v1-" + "a1" * 20


class FakeResponse:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.raw


class OpenRouterConfigTests(unittest.TestCase):
    def test_variable_selects_openrouter_with_automatic_model(self):
        config = load_config({"OPENROUTER_API_KEY": KEY})
        self.assertEqual((config.provider, config.model, config.ready), ("openrouter", "anthropic/claude-sonnet-4.6", True))
        self.assertEqual(config.public_state()["provider"], "OpenRouter")
        chosen = load_config({"OPENROUTER_API_KEY": KEY, "BUBUS_AI_MODEL": "openai/gpt-5"})
        self.assertEqual(chosen.model, "openai/gpt-5")
        self.assertIn("OpenRouter", load_config({}).public_state()["reason"])

    def test_request_goes_to_openrouter_and_errors_are_in_french(self):
        seen = []

        def fake_open(request, timeout):
            seen.append(request)
            return FakeResponse({"choices": [{"message": {"content": " Bonjour "}}]})

        config = ChatConfig("openrouter", "anthropic/claude-sonnet-4.6", KEY, 5)
        with patch.object(assistant_chat.urllib.request, "urlopen", fake_open):
            text = assistant_chat._call_chat_completions(config=config, system="s",
                                               messages=[{"role": "user", "content": "Salut"}], snapshot="relevé")
        self.assertEqual(text, "Bonjour")
        self.assertEqual(seen[0].full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(seen[0].get_header("Authorization"), f"Bearer {KEY}")
        self.assertEqual(json.loads(seen[0].data)["model"], "anthropic/claude-sonnet-4.6")

        def refuse(code):
            def opener(request, timeout):
                raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(b"{}"))
            return opener

        for code, words in ((401, "clé OpenRouter est refusée"), (402, "crédit OpenRouter"), (404, "refuse le modèle")):
            with patch.object(assistant_chat.urllib.request, "urlopen", refuse(code)):
                with self.assertRaises(ChatUnavailable) as error:
                    assistant_chat._call_chat_completions(config=config, system="s",
                                                messages=[{"role": "user", "content": "x"}], snapshot="r")
            self.assertIn(words, str(error.exception))


class OpenRouterKeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keys = OpenRouterKeyStore(Path(self.tmp.name), protector=FakeProtector())

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_is_encrypted_validated_and_never_exposed(self):
        self.assertEqual(self.keys.state()["saved"], False)
        for bad in ("", "sk-ant-123", "sk-or-court", KEY + " ; rm", 12):
            with self.assertRaises(AIKeyError):
                self.keys.save(bad)
        with self.assertRaises(AIKeyError):
            self.keys.save(KEY, "pas un modèle")
        state = self.keys.save(f"  {KEY}  ", "anthropic/claude-opus-4.5")
        self.assertEqual((state["saved"], state["model"]), (True, "anthropic/claude-opus-4.5"))
        self.assertNotIn(KEY, json.dumps(state))
        self.assertNotIn(KEY.encode(), (Path(self.tmp.name) / "openrouter-key.bin").read_bytes())
        self.assertEqual(self.keys.credentials(), (KEY, "anthropic/claude-opus-4.5"))
        self.assertEqual(self.keys.forget()["saved"], False)
        self.assertIsNone(self.keys.credentials())

    def test_saved_key_is_used_only_when_no_server_key(self):
        call = RecordingCall("Salut !")
        service = ChatService(config_loader=lambda: load_config({}), openai_call=call, openrouter_keys=self.keys)
        self.assertFalse(service.state()["ready"])
        self.keys.save(KEY)
        state = service.state()
        self.assertEqual((state["ready"], state["provider"], state["model"]), (True, "OpenRouter", "anthropic/claude-sonnet-4.6"))
        result = service.reply([{"role": "user", "content": "Salut"}], "relevé")
        self.assertEqual((result["reply"], result["provider"]), ("Salut !", "OpenRouter"))
        self.assertEqual(call.calls[0]["config"].api_key, KEY)


class LocalOpenRouterRouteTests(_ServerCase):
    def setUp(self):
        from bubus.server import DeskHandler
        self.handler_class = DeskHandler
        super().setUp()
        self.server.openrouter_keys = OpenRouterKeyStore(Path(self.tmp.name), protector=FakeProtector())

    def post(self, path, payload, origin=None):
        host = f"127.0.0.1:{self.port}"
        return self.request("POST", path, json.dumps(payload).encode(), {
            "Host": host, "Content-Type": "application/json", "Origin": origin or f"http://{host}"})

    def test_save_and_forget_from_this_pc_only(self):
        self.assertEqual(self.post("/api/desk/openrouter/key", {"key": KEY}, "https://evil.example")[0], 403)
        self.assertEqual(self.post("/api/desk/openrouter/key", {"key": "faux"})[0], 400)
        self.assertEqual(self.post("/api/desk/openrouter/key", {"key": KEY, "extra": 1})[0], 400)
        status, _, body = self.post("/api/desk/openrouter/key", {"key": KEY, "model": ""})
        self.assertEqual(status, 200, body)
        self.assertNotIn(KEY, body.decode() if isinstance(body, bytes) else body)
        self.assertEqual(self.post("/api/desk/openrouter/forget", {})[0], 400)
        status, _, body = self.post("/api/desk/openrouter/forget", {"forget": True})
        self.assertFalse(json.loads(body)["saved"])


class CloudOpenRouterRouteTests(_ServerCase):
    def test_online_version_refuses_key_entry(self):
        self.assertIsNone(self.server.openrouter_keys)
        cookie = self.session_cookie()
        status, _, _ = self.request("POST", "/api/desk/openrouter/key", json.dumps({"key": KEY}).encode(), {
            "Cookie": cookie, "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
