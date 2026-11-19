import json
from unittest.mock import patch

from bubus.ai_connections import AIConnections, AIKeyError, PROVIDERS
from bubus.assistant_chat import ChatService, load_config, _call_chat_completions
from bubus.cloud_preview import _CloudTokenProtector
from tests.bubus_v2.test_cloud_app import _ServerCase, RecordingCall
from tests.bubus_v2.test_openrouter import FakeResponse


class ConnectionsTests(_ServerCase):
    def setUp(self):
        super().setUp()
        self.store = AIConnections(self.tmp.name, _CloudTokenProtector("ai-keys/v1"))
        self.server.ai_connections = self.store
        self.call = RecordingCall()
        self.server.chat_service = ChatService(connections=self.store, config_loader=lambda: load_config({}),
                                               openai_call=self.call)

    def post(self, data, cookie=None, site="same-origin"):
        headers = {"Content-Type": "application/json", "Sec-Fetch-Site": site}
        if cookie:
            headers["Cookie"] = cookie
        return self.request("POST", "/api/desk/ai-connections", json.dumps(data), headers)

    def test_import_select_and_chat_without_exposing_keys(self):
        secret = "example-key-for-test-only"
        keys = {name: {"key": secret, "model": ""} for name in PROVIDERS}
        data = {"action": "save", "keys": keys}
        cookie = self.session_cookie()
        self.assertEqual(self.post(data)[0], 401)
        self.assertEqual(self.post(data, cookie, "cross-site")[0], 403)
        status, _, body = self.post(data, cookie)
        self.assertEqual(status, 200)
        self.assertNotIn(secret.encode(), body)
        self.assertEqual(len(json.loads(body)["providers"]), len(keys))
        self.assertNotIn(secret.encode(), self.store.path.read_bytes())
        self.assertEqual(self.post({"action": "select", "provider": "zai"}, cookie)[0], 200)
        result = self.server.chat_service.reply([{"role": "user", "content": "Salut"}], "Cours")
        self.assertEqual(result["provider"], "Z.ai · GLM")
        self.assertEqual(self.call.calls[0]["config"].api_key, secret)
        again = AIConnections(self.tmp.name, _CloudTokenProtector("ai-keys/v1"))
        self.assertEqual(again.selected()[0], "zai")
        self.assertEqual(self.post({"action": "select", "provider": "cursor"}, cookie)[0], 400)
        self.assertEqual(self.post({"action": "forget", "provider": "zai"}, cookie)[0], 200)
        self.assertIsNone(again.selected())

    def test_invalid_import_is_atomic(self):
        data = {"action": "save", "keys": {"openrouter": {"key": "sample-key-value", "model": ""},
                                          "kimi": {"key": "bad", "model": ""}}}
        with self.assertRaises(AIKeyError):
            self.store.update(data)
        self.assertFalse(any(r["saved"] for r in self.store.state()["providers"]))

    def test_each_new_chat_api_uses_its_own_endpoint(self):
        for name in ("zai", "deepseek", "kimi", "groq"):
            config = load_config({PROVIDERS[name].variable: "fake-personal-key"})
            with patch("urllib.request.urlopen", return_value=FakeResponse({
                    "choices": [{"message": {"content": "Bonjour"}}]})) as send:
                reply = _call_chat_completions(config=config, system="s", messages=[{"role": "user", "content": "ok"}], snapshot="s")
            self.assertEqual(reply, "Bonjour")
            self.assertEqual(send.call_args.args[0].full_url, PROVIDERS[name].endpoint)
            self.assertNotIn("fake-personal-key", send.call_args.args[0].full_url)
