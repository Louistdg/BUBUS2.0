"""Lexical search and status summaries."""
from __future__ import annotations

import re
import sqlite3


def _fts_query(query: str) -> str:
    terms = re.findall(r'"([^"]+)"|([^\s]+)', query)
    tokens = [phrase or word for phrase, word in terms]
    tokens = [token.replace('"', '""') for token in tokens if token]
    if not tokens:
        raise ValueError('La recherche doit contenir au moins un terme.')
    return ' AND '.join(f'"{token}"' for token in tokens)


def search(db: sqlite3.Connection, query: str, limit: int = 10) -> list[sqlite3.Row]:
    match = _fts_query(query)
    return db.execute('''SELECT d.title,d.document_id,dv.version,c.chunk_id,c.ordinal,c.heading,c.line_start,c.line_end,
                         c.text,dv.original_path,dv.sha256,s.source_type,
                         bm25(chunks_fts,0.0,1.0,1.0) AS score
      FROM chunks_fts JOIN chunks c ON c.chunk_id=chunks_fts.chunk_id
      JOIN document_versions dv ON dv.document_version_id=c.document_version_id
      JOIN documents d ON d.document_id=dv.document_id JOIN sources s ON s.source_id=dv.source_id
      WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?''', (match, max(1, min(limit, 100)))).fetchall()


def status(db: sqlite3.Connection) -> dict[str, int | str]:
    return {
        'documents': db.execute('SELECT count(*) FROM documents').fetchone()[0],
        'versions': db.execute('SELECT count(*) FROM document_versions').fetchone()[0],
        'chunks': db.execute('SELECT count(*) FROM chunks').fetchone()[0],
        'sources': db.execute('SELECT count(*) FROM sources').fetchone()[0],
        'failed_runs': db.execute("SELECT count(*) FROM ingestion_runs WHERE status='FAILED'").fetchone()[0],
        'fts5': 'available' if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='chunks_fts'").fetchone()[0] else 'unavailable',
    }
