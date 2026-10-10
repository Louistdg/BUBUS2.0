"""Small loopback-only, read-only INVEST-AI progress dashboard."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit


ROOT = Path(__file__).resolve().parents[3]
COORDINATION_DB = ROOT / "data" / "coordination" / "coordination.sqlite3"
KNOWLEDGE_DB = ROOT / "data" / "knowledge" / "knowledge.sqlite3"
ASSISTANT_DIR = Path(__file__).resolve().parent
AGENT_IDS = tuple(f"CODEX-{letter}" for letter in "ABCDE")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|api[_ -]?key|access[_ -]?token|token|authorization|"
    r"private[_ -]?key|mnemonic|seed[_ -]?phrase|recovery[_ -]?phrase|credential)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
SECRET_TOKEN = re.compile(
    r"(?i)(?:sk-(?:proj-)?[a-z0-9]{24,}|gh[pousr]_[a-z0-9_]{20,}|github_pat_[a-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16}|Bearer\s+[a-z0-9._~+/-]{16,})"
)
PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)


class DashboardError(RuntimeError):
    """Safe-to-report dashboard data or configuration failure."""


def _readonly_connection(path: Path) -> sqlite3.Connection:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DashboardError("A required local data source is unavailable.") from exc
    if not resolved.is_file():
        raise DashboardError("A required local data source is unavailable.")
    uri = "file:" + quote(str(resolved).replace("\\", "/"), safe="/:") + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=2.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA query_only").fetchone()[0] != 1:
            db.close()
            raise DashboardError("Read-only database mode could not be enforced.")
        db.execute("BEGIN")
        return db
    except (sqlite3.Error, OSError) as exc:
        raise DashboardError("A required local data source could not be read.") from exc


def _safe_text(value: object, limit: int = 500) -> str:
    text = str(value or "")
    text = PRIVATE_KEY_BLOCK.sub("[secret redacted]", text)
    text = SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = SECRET_TOKEN.sub("[secret redacted]", text)
    return text[:limit]


def _path_is_link(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            return True
    return False


def _validated_report_path(raw_path: object, root: Path) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    handoff_root = root / "handoffs"
    if handoff_root.is_symlink() or (hasattr(handoff_root, "is_junction") and handoff_root.is_junction()):
        return None
    handoffs = handoff_root.resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(handoffs)
    except (OSError, ValueError):
        return None
    if resolved.suffix.casefold() != ".md" or not resolved.is_file() or _path_is_link(resolved, handoffs):
        return None
    return resolved


def _paths_overlap(left: str, right: str) -> bool:
    a = left.replace("\\", "/").rstrip("/").casefold()
    b = right.replace("\\", "/").rstrip("/").casefold()
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _coordination_verdict(mission: dict, missions: dict[str, dict], dependencies: list[dict],
                          claims: list[dict], requirements: list[dict], observations: dict[str, dict]) -> dict:
    reasons: list[str] = []
    for dependency in dependencies:
        dep_id = dependency["depends_on"]
        dep_status = missions.get(dep_id, {}).get("status", "MISSING")
        kind = dependency["dependency_type"]
        if kind == "REQUIRES" and dep_status != "VALIDATED":
            reasons.append(f"requires {dep_id} VALIDATED (currently {dep_status})")
        elif kind == "BLOCKS" and dep_status not in {"VALIDATED", "CLOSED"}:
            reasons.append(f"blocked by {dep_id} ({dep_status})")

    own_claims = [claim for claim in claims if claim["mission_id"] == mission["mission_id"]]
    other_claims = [claim for claim in claims if claim["mission_id"] != mission["mission_id"]]
    claim_conflicts: list[str] = []
    for own in own_claims:
        for other in other_claims:
            if own["scope_type"] != other["scope_type"]:
                continue
            same_scope = (_paths_overlap(own["scope"], other["scope"])
                          if own["scope_type"] == "PATH"
                          else own["scope"].casefold() == other["scope"].casefold())
            if not same_scope or (own["mode"] == other["mode"] == "READ"):
                continue
            if "EXCLUSIVE" in {own["mode"], other["mode"]} or (own["mode"] == other["mode"] == "WRITE"):
                claim_conflicts.append(f"claim conflict: {own['scope']} ({other['mission_id']})")
    reasons.extend(claim_conflicts)

    warnings: list[str] = []
    resource_conflicts: list[str] = []
    mission_requirements = [item for item in requirements if item["mission_id"] == mission["mission_id"]]
    for requirement in mission_requirements:
        name, mode = requirement["resource_name"], requirement["mode"]
        observation = observations.get(name)
        if observation is None and mode != "EXCLUSIVE":
            warnings.append(f"resource not observed: {name}")
            continue
        if mode == "FORBIDDEN":
            if observation["value"] is None:
                warnings.append(f"forbidden resource cannot be evaluated: {name}")
            elif observation["value"] != 0:
                resource_conflicts.append(f"forbidden resource active: {name}")
        elif mode == "THRESHOLD":
            if observation["unit"] != requirement["unit"] or observation["value"] is None:
                warnings.append(f"resource threshold cannot be evaluated: {name}")
                continue
            actual, threshold, op = observation["value"], requirement["threshold_value"], requirement["threshold_operator"]
            passed = {">=": actual >= threshold, "<=": actual <= threshold,
                      ">": actual > threshold, "<": actual < threshold}.get(op, False)
            if not passed:
                warnings.append(f"resource threshold unmet: {name} {op} {threshold} {requirement['unit']}")
        elif mode == "EXCLUSIVE":
            held = any(item["scope_type"] == "RESOURCE" and item["scope"] == name
                       and item["mission_id"] != mission["mission_id"] for item in claims)
            required_by_active = any(item["resource_name"] == name and item["mode"] == "EXCLUSIVE"
                                     and item["mission_id"] != mission["mission_id"]
                                     and missions.get(item["mission_id"], {}).get("status") == "ACTIVE"
                                     for item in requirements)
            if held or required_by_active:
                resource_conflicts.append(f"exclusive resource in use: {name}")
    reasons.extend(resource_conflicts)
    reasons.extend(warnings)
    status = mission["status"]
    if status in {"BLOCKED", "RED"}:
        reasons.append(f"mission status is {status}")
    hard_block = bool(claim_conflicts or resource_conflicts or status in {"BLOCKED", "RED"}
                      or any(reason.startswith("requires ") or reason.startswith("blocked by ") for reason in reasons))
    color = "RED" if hard_block else "YELLOW" if warnings or status == "YELLOW" else "GREEN"
    return {"color": color, "may_start": color == "GREEN", "reasons": reasons}


def build_dashboard_data(coordination_path: Path = COORDINATION_DB, knowledge_path: Path = KNOWLEDGE_DB,
                         root: Path = ROOT) -> dict:
    coord = _readonly_connection(coordination_path)
    try:
        knowledge = _readonly_connection(knowledge_path)
    except Exception:
        coord.rollback()
        coord.close()
        raise
    try:
        agents = [dict(row) for row in coord.execute("SELECT agent_id,role FROM agents ORDER BY agent_id")
                  if row["agent_id"] in AGENT_IDS]
        all_mission_rows = [dict(row) for row in coord.execute(
            "SELECT mission_id,agent_id,title,status,priority,created_at,updated_at,handoff_path "
            "FROM missions ORDER BY agent_id,priority,mission_id")]
        missions_by_id = {row["mission_id"]: row for row in all_mission_rows}
        mission_rows = [row for row in all_mission_rows if row["agent_id"] in AGENT_IDS]
        dependencies = [dict(row) for row in coord.execute(
            "SELECT mission_id,depends_on,dependency_type FROM dependencies ORDER BY mission_id,depends_on")]
        claims = [dict(row) for row in coord.execute(
            "SELECT mission_id,scope_type,scope,mode FROM claims WHERE released_at IS NULL")]
        requirements = [dict(row) for row in coord.execute(
            "SELECT mission_id,resource_name,mode,threshold_operator,threshold_value,unit FROM resource_requirements")]
        observations = {row["resource_name"]: dict(row) for row in coord.execute(
            "SELECT resource_name,value,unit,observed_at FROM resource_observations")}
        evidence_rows = [dict(row) for row in coord.execute(
            "SELECT mission_id,evidence_type,path_or_reference,sha256,created_at,agent_id "
            "FROM evidence ORDER BY created_at,evidence_id")]

        dep_by_mission: dict[str, list[dict]] = {mission_id: [] for mission_id in missions_by_id}
        for dep in dependencies:
            if dep["mission_id"] in dep_by_mission:
                dep_by_mission[dep["mission_id"]].append(dep)
        req_by_mission: dict[str, list[dict]] = {mission_id: [] for mission_id in missions_by_id}
        for req in requirements:
            if req["mission_id"] in req_by_mission:
                req_by_mission[req["mission_id"]].append(req)

        display_missions: list[dict] = []
        alerts: list[dict] = []
        report_records: dict[str, dict] = {}
        for row in mission_rows:
            verdict = _coordination_verdict(row, missions_by_id, dep_by_mission[row["mission_id"]], claims,
                                            requirements, observations)
            item = {
                "mission_id": _safe_text(row["mission_id"], 120),
                "agent_id": row["agent_id"],
                "title": _safe_text(row["title"], 240),
                "status": _safe_text(row["status"], 60),
                "priority": _safe_text(row["priority"], 40),
                "updated_at": row["updated_at"],
                "coordination_verdict": verdict["color"],
                "may_start": verdict["may_start"],
                "verdict_reasons": [_safe_text(reason, 240) for reason in verdict["reasons"]],
                "dependencies": [],
                "reports": [],
            }
            for dependency in dep_by_mission[row["mission_id"]]:
                dep_row = missions_by_id.get(dependency["depends_on"])
                item["dependencies"].append({
                    "mission_id": _safe_text(dependency["depends_on"], 120),
                    "dependency_type": dependency["dependency_type"],
                    "status": _safe_text(dep_row["status"], 60) if dep_row else "MISSING",
                })
            raw_reports = []
            if row["handoff_path"]:
                raw_reports.append(("MISSION_HANDOFF", row["handoff_path"], None, row["updated_at"], row["agent_id"]))
            raw_reports.extend((e["evidence_type"], e["path_or_reference"], e["sha256"], e["created_at"], e["agent_id"])
                               for e in evidence_rows if e["mission_id"] == row["mission_id"])
            for kind, raw_path, digest, created_at, agent_id in raw_reports:
                safe_path = _validated_report_path(raw_path, root)
                if safe_path is None:
                    continue
                relative = safe_path.relative_to((root / "handoffs").resolve()).as_posix()
                key = relative.casefold()
                record = report_records.setdefault(key, {
                    "report_id": key,
                    "name": _safe_text(safe_path.name, 200),
                    "relative_path": _safe_text(relative, 400),
                    "modified_at": datetime.fromtimestamp(safe_path.stat().st_mtime, timezone.utc).isoformat(),
                    "sha256": digest if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) else None,
                    "references": [],
                })
                record["references"].append({"mission_id": item["mission_id"], "agent_id": agent_id,
                                              "kind": kind, "created_at": created_at})
                item["reports"].append(key)
            if verdict["color"] != "GREEN" or row["status"] in {
                    "BLOCKED", "RED", "CORRECTION_REQUIRED", "WAITING_FOR_RESOURCES"}:
                alerts.append({"mission_id": item["mission_id"], "agent_id": row["agent_id"],
                               "title": item["title"], "status": item["status"],
                               "coordination_verdict": verdict["color"],
                               "reasons": item["verdict_reasons"]})
            display_missions.append(item)

        agent_cards = []
        for agent in agents:
            agent_missions = [item for item in display_missions if item["agent_id"] == agent["agent_id"]]
            agent_cards.append({"agent_id": agent["agent_id"], "role": _safe_text(agent["role"], 240),
                                "mission_count": len(agent_missions),
                                "validated_count": sum(item["status"] == "VALIDATED" for item in agent_missions),
                                "red_count": sum(item["coordination_verdict"] == "RED" for item in agent_missions),
                                "missions": agent_missions})

        knowledge_counts = {
            "documents": knowledge.execute("SELECT count(*) FROM documents").fetchone()[0],
            "versions": knowledge.execute("SELECT count(*) FROM document_versions").fetchone()[0],
            "chunks": knowledge.execute("SELECT count(*) FROM chunks").fetchone()[0],
            "fts5": bool(knowledge.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='chunks_fts'").fetchone()[0]),
            "latest_ingested_at": (knowledge.execute("SELECT max(ingested_at) FROM document_versions").fetchone()[0]),
        }
        status_counts: dict[str, int] = {}
        for mission in display_missions:
            status_counts[mission["status"]] = status_counts.get(mission["status"], 0) + 1
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agents": agent_cards,
            "mission_count": len(display_missions),
            "status_counts": status_counts,
            "alerts": alerts,
            "reports": sorted(report_records.values(), key=lambda report: (report["name"].casefold(), report["relative_path"].casefold())),
            "knowledge": knowledge_counts,
        }
    except sqlite3.Error as exc:
        raise DashboardError("The local project status could not be read.") from exc
    finally:
        coord.rollback()
        coord.close()
        knowledge.rollback()
        knowledge.close()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "InvestAIDashboard/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _host_is_local(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_port}"
        return self.headers.get("Host", "").casefold() == expected.casefold()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        if not self._host_is_local():
            self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            return
        path = urlsplit(self.path).path
        if path == "/":
            asset = ASSISTANT_DIR / "dashboard.html"
            content_type = "text/html; charset=utf-8"
        elif path == "/assets/dashboard.css":
            asset = ASSISTANT_DIR / "dashboard.css"
            content_type = "text/css; charset=utf-8"
        elif path == "/assets/dashboard.js":
            asset = ASSISTANT_DIR / "dashboard.js"
            content_type = "text/javascript; charset=utf-8"
        elif path == "/api/dashboard":
            try:
                result = build_dashboard_data()
                body = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except DashboardError:
                body = b'{"error":"Local dashboard data is unavailable."}'
                self._send(503, body, "application/json; charset=utf-8")
                return
            self._send(200, body, "application/json; charset=utf-8")
            return
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            body = asset.read_bytes()
        except OSError:
            self._send(503, b"Dashboard asset unavailable", "text/plain; charset=utf-8")
            return
        self._send(200, body, content_type)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        if not self._host_is_local():
            self._send(403, b"", "text/plain; charset=utf-8")
            return
        path = urlsplit(self.path).path
        if path == "/":
            asset, content_type = ASSISTANT_DIR / "dashboard.html", "text/html; charset=utf-8"
        elif path == "/assets/dashboard.css":
            asset, content_type = ASSISTANT_DIR / "dashboard.css", "text/css; charset=utf-8"
        elif path == "/assets/dashboard.js":
            asset, content_type = ASSISTANT_DIR / "dashboard.js", "text/javascript; charset=utf-8"
        else:
            self._send(404, b"", "text/plain; charset=utf-8")
            return
        try:
            self._send(200, b"", content_type)
        except OSError:
            self._send(503, b"", "text/plain; charset=utf-8")

    def _method_not_allowed(self) -> None:
        self._send(405, b"Method not allowed", "text/plain; charset=utf-8")

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed


def make_server(port: int = 8765) -> ThreadingHTTPServer:
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535.")
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local read-only INVEST-AI progress dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="do not open the local page automatically")
    args = parser.parse_args(argv)
    try:
        server = make_server(args.port)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Dashboard could not start: {exc}\n")
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"INVEST-AI local dashboard: {url}")
    print("Lecture seule; arrêt avec Ctrl+C.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url, new=1, autoraise=False)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
