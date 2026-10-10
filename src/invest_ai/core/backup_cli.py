"""Command line entry point for a Knowledge Core backup."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from invest_ai.core.backup import backup_knowledge_database
from invest_ai.core.paths import RuntimePaths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sauvegarde cohérente read-only du Knowledge Core")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args(argv)
    paths = RuntimePaths.from_root(args.root)
    result = backup_knowledge_database(paths.knowledge_db, paths.backups_dir)
    print(json.dumps({"status": "BACKUP_CREATED", "directory": str(result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
