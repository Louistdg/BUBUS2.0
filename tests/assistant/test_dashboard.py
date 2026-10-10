from __future__ import annotations

import hashlib
import http.client
import json
import re
import sqlite3
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from invest_ai.assistant.dashboard import (  # noqa: E402
    COORDINATION_DB,
    KNOWLEDGE_DB,
    ROOT as PROJECT_ROOT,
    _readonly_connection,
    _safe_text,
    _validated_report_path,
    build_dashboard_data,
    make_server,
)
from invest_ai.coordination.models import MissionStatus


def related_files(database: Path) -> list[Path]:
    return [database, Path(str(database) + "-wal")]


def hashes(paths: list[Path]) -> dict[str, str | None]:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            for path in paths}


class DashboardReadOnlyTests(unittest.TestCase):
    def test_both_production_databases_are_read_only_and_unchanged(self):
        files = related_files(COORDINATION_DB) + related_files(KNOWLEDGE_DB)
        before = hashes(files)
        for path in (COORDINATION_DB, KNOWLEDGE_DB):
            db = _readonly_connection(path)
            try:
                self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("CREATE TABLE dashboard_must_not_write(value TEXT)")
                table, column = ("missions", "mission_id") if path == COORDINATION_DB else ("documents", "document_id")
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute(f"UPDATE {table} SET {column}={column} WHERE 0")
            finally:
                db.rollback()
                db.close()
        build_dashboard_data()
        self.assertEqual(before, hashes(files))

    def test_dashboard_exposes_only_five_codex_and_distinguishes_verdict_from_validation(self):
        data = build_dashboard_data()
        self.assertEqual([agent["agent_id"] for agent in data["agents"]],
                         ["CODEX-A", "CODEX-B", "CODEX-C", "CODEX-D", "CODEX-E"])
        e_missions = next(agent["missions"] for agent in data["agents"] if agent["agent_id"] == "CODEX-E")
        dashboard_mission = next(row for row in e_missions if row["mission_id"] == "INVEST-AI-LOCAL-DASHBOARD-01")
        self.assertIn(dashboard_mission["status"], {status.value for status in MissionStatus})
        self.assertEqual(dashboard_mission["coordination_verdict"], "GREEN")
        all_missions = [mission for agent in data["agents"] for mission in agent["missions"]]
        validated = next(row for row in all_missions if row["status"] == "VALIDATED")
        self.assertEqual(validated["coordination_verdict"], "GREEN")
        self.assertIn("knowledge", data)
        self.assertIn("reports", data)

    def test_untrusted_display_text_redacts_secret_patterns(self):
        sample = "ordinary <script>alert(1)</script> api_key=sample-secret sk-123456789012345678901234567890"
        rendered = _safe_text(sample)
        self.assertIn("<script>", rendered)
        self.assertNotIn("sample-secret", rendered)
        self.assertNotIn("sk-123456789012345678901234567890", rendered)

    def test_report_path_must_be_referenced_path_under_handoffs_and_markdown(self):
        allowed = PROJECT_ROOT / "handoffs" / "CODEX-E" / "LLM-BENCHMARK-01.md"
        if allowed.is_file():
            self.assertEqual(_validated_report_path(str(allowed), PROJECT_ROOT), allowed.resolve())
        self.assertIsNone(_validated_report_path(str(PROJECT_ROOT / "START-HERE.md"), PROJECT_ROOT))
        self.assertIsNone(_validated_report_path(str(PROJECT_ROOT / "handoffs" / "missing.txt"), PROJECT_ROOT))
        traversal = PROJECT_ROOT / "handoffs" / ".." / "START-HERE.md"
        self.assertIsNone(_validated_report_path(str(traversal), PROJECT_ROOT))

    def test_api_only_contains_whitelisted_fields_and_no_free_form_notes(self):
        payload = json.dumps(build_dashboard_data(), ensure_ascii=False)
        for forbidden in ("resource_observations", "private_key", "evidence_description", "handoff_contents", "notes"):
            self.assertNotIn(forbidden, payload.casefold())
        self.assertNotRegex(payload, re.compile(r"(?:sk-(?:proj-)?[a-z0-9]{24,}|gh[pousr]_[a-z0-9_]{20,})", re.I))

    def test_static_page_has_no_external_resources(self):
        static = "\n".join((ROOT / "src" / "invest_ai" / "assistant" / name).read_text(encoding="utf-8")
                            for name in ("dashboard.html", "dashboard.css", "dashboard.js"))
        self.assertNotRegex(static, re.compile(r"https?://", re.I))

    def test_dashboard_fetches_at_load_and_manual_refresh_without_html_injection(self):
        script = (ROOT / "src" / "invest_ai" / "assistant" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn('refresh();', script)
        self.assertIn('addEventListener("click", refresh)', script)
        self.assertNotIn("innerHTML", script)
        self.assertIn("textContent", script)


class DashboardHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method: str, path: str = "/", host: str | None = None) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        response = connection.getresponse()
        result = response.status, {key.casefold(): value for key, value in response.getheaders()}, response.read()
        connection.close()
        return result

    def test_loopback_page_and_api_work_with_security_headers(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Suivi du projet", body)
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        api_status, api_headers, api_body = self.request("GET", "/api/dashboard")
        self.assertEqual(api_status, 200)
        self.assertEqual(api_headers["cache-control"], "no-store")
        self.assertEqual(json.loads(api_body)["agents"][0]["agent_id"], "CODEX-A")

    def test_rejects_non_loopback_host_and_non_get_methods(self):
        status, _, _ = self.request("GET", "/api/dashboard", host="example.test")
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/dashboard", host=f"localhost:{self.port}")
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/dashboard")
        self.assertEqual(status, 405)
        status, _, _ = self.request("GET", "/unknown")
        self.assertEqual(status, 404)

    def test_report_or_arbitrary_file_routes_are_not_served(self):
        status, _, _ = self.request("GET", "/D:/PRIVATE-PROJECT/START-HERE.md")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
