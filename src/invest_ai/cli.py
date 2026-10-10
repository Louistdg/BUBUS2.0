"""Minimal command line interface for the local document memory."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from invest_ai.knowledge.ingest import IngestionError, ingest_file
from invest_ai.knowledge.search import search, status
from invest_ai.storage.database import connect, fts5_available, initialize

ROOT = Path(__file__).resolve().parents[2]
DB_DEFAULT = ROOT / 'data' / 'knowledge' / 'knowledge.sqlite3'
KNOWLEDGE_ROOT = ROOT / 'knowledge'


def _db(path: Path) -> sqlite3.Connection:
    db = connect(path)
    initialize(db)
    return db


def _ingest(db: sqlite3.Connection, path: Path, source_type: str) -> int:
    try:
        result = ingest_file(db, path, KNOWLEDGE_ROOT, source_type=source_type)
    except (IngestionError, OSError) as exc:
        print(f'ERREUR {path}: {exc}', file=sys.stderr)
        return 1
    print(f'{result.status}: {result.title} | version={result.version} | chunks={result.chunks} | sha256={result.sha256}')
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(prog='invest-ai', description='Mémoire documentaire locale INVEST-AI')
    parser.add_argument('--db', type=Path, default=DB_DEFAULT, help='chemin SQLite (défaut: data/knowledge/knowledge.sqlite3)')
    sub = parser.add_subparsers(dest='area', required=True)
    knowledge = sub.add_parser('knowledge', help='gestion de la mémoire documentaire')
    actions = knowledge.add_subparsers(dest='action', required=True)
    actions.add_parser('init', help='initialiser la base et l’index lexical')
    ingest = actions.add_parser('ingest', help='importer un fichier .txt ou .md')
    ingest.add_argument('file', type=Path)
    ingest.add_argument('--source-type', default='user_document')
    ingest_dir = actions.add_parser('ingest-dir', help='importer récursivement les .txt et .md d’un dossier')
    ingest_dir.add_argument('directory', type=Path)
    ingest_dir.add_argument('--source-type', default='user_document')
    find = actions.add_parser('search', help='rechercher des termes ou expressions exactes')
    find.add_argument('query')
    find.add_argument('--limit', type=int, default=10)
    actions.add_parser('status', help='afficher les compteurs de la base')
    coord = sub.add_parser('coord', help='registre local de coordination multi-IA')
    coord.add_argument('coord_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.area == 'coord':
        from invest_ai.coordination.cli import main as coordination_main
        return coordination_main(args.coord_args)
    db = _db(args.db)
    try:
        if args.action == 'init':
            print(f'Base initialisée: {args.db.resolve()} | FTS5={"oui" if fts5_available(db) else "non"}')
            return 0 if fts5_available(db) else 2
        if args.action == 'ingest':
            return _ingest(db, args.file, args.source_type)
        if args.action == 'ingest-dir':
            directory = args.directory.resolve(strict=True)
            if not directory.is_dir():
                print('ERREUR: le chemin doit être un dossier.', file=sys.stderr)
                return 1
            files = sorted(p for p in directory.rglob('*') if p.is_file() and p.suffix.lower() in {'.md','.txt'})
            failures = sum(_ingest(db, file, args.source_type) for file in files)
            print(f'Import dossier terminé: {len(files)} fichier(s), {failures} erreur(s).')
            return 1 if failures else 0
        if args.action == 'search':
            results = search(db, args.query, args.limit)
            if not results:
                print('Aucun résultat.')
                return 0
            for row in results:
                excerpt = ' '.join(row['text'].split())
                if len(excerpt) > 360:
                    excerpt = excerpt[:357] + '...'
                print(f"{row['title']} | document={row['document_id']} | v{row['version']} | chunk={row['ordinal']} | lignes={row['line_start']}-{row['line_end']}\n"
                      f"source={row['original_path']} | type={row['source_type']} | sha256={row['sha256']}\n{excerpt}\n")
            return 0
        if args.action == 'status':
            print(f'Base: {args.db.resolve()}')
            for key, value in status(db).items():
                print(f'{key}: {value}')
            return 0
        return 2
    except (sqlite3.Error, ValueError, OSError) as exc:
        print(f'ERREUR: {exc}', file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
