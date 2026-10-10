"""Read-only status views for the coordination registry."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from invest_ai.coordination import CoordinationStore, bootstrap_initial_state

ROOT = Path(__file__).resolve().parents[3]
DB_DEFAULT = ROOT / "data" / "coordination" / "coordination.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invest-ai coord")
    parser.add_argument("--db", type=Path, default=DB_DEFAULT)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("init", help="bootstrap idempotent de l'état initial documenté")
    actions.add_parser("status")
    actions.add_parser("agents")
    actions.add_parser("missions")
    mission = actions.add_parser("mission")
    mission.add_argument("mission_id")
    actions.add_parser("claims")
    actions.add_parser("blockers")
    evidence = actions.add_parser("evidence")
    evidence.add_argument("mission_id")
    actions.add_parser("resources")
    events = actions.add_parser("history")
    events.add_argument("--mission")
    args = parser.parse_args(argv)
    try:
        store = CoordinationStore(args.db)
        if args.action == "init":
            bootstrap_initial_state(store)
            print(f"Coordination initialisée: {args.db.resolve()}")
            return 0
        views = {
            "status": store.status_summary,
            "agents": store.list_agents,
            "missions": store.list_missions,
            "claims": store.list_claims,
            "blockers": store.blockers,
            "resources": store.resources,
        }
        if args.action == "mission":
            result = store.mission(args.mission_id)
        elif args.action == "evidence":
            result = store.evidence_for(args.mission_id)
        elif args.action == "history":
            result = store.events(args.mission)
        else:
            result = views[args.action]()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        return 2
