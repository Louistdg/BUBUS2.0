"""Deterministic, provenance-preserving ingestion for plain text and Markdown."""
from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from invest_ai.storage.database import initialize

ALLOWED_TYPES = {'official','documentation','paper','github','forum','codex_report','user_document','other'}
NAMESPACE = uuid.UUID('5b3d816a-d922-4d52-a11f-1f5a684efdaa')


class IngestionError(ValueError):
    """Input cannot safely be ingested; message is suitable for CLI display."""


@dataclass(frozen=True)
class IngestResult:
    status: str
    document_id: str | None
    version: int | None
    sha256: str
    chunks: int
    title: str


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('._')
    return cleaned[:120] or 'document'


def _normalize(raw: bytes, path: Path) -> str:
    if path.suffix.lower() not in {'.txt', '.md'}:
        raise IngestionError(f"Format non pris en charge : {path.suffix or '(sans extension)'}. Formats acceptés : .txt, .md")
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise IngestionError(f"Fichier non UTF-8 valide (octet {exc.start}); source conservée sans modification.") from exc
    text = unicodedata.normalize('NFC', text.replace('\r\n', '\n').replace('\r', '\n'))
    if not text.strip():
        raise IngestionError('Document vide; aucune version créée.')
    return text


def _chunks(text: str, limit: int = 1800) -> list[tuple[str | None, int, int, str]]:
    """Group paragraphs deterministically, retaining heading and source lines."""
    lines = text.splitlines()
    result: list[tuple[str | None, int, int, str]] = []
    heading: str | None = None
    buf: list[str] = []
    start = 1
    end = 0

    def flush() -> None:
        nonlocal buf, start, end
        body = '\n'.join(buf).strip()
        if body:
            result.append((heading, start, end, body))
        buf = []

    for number, line in enumerate(lines, 1):
        match = re.match(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$', line)
        if match:
            flush()
            heading = match.group(1).strip()
            start = number
            end = number
            buf = [line]
            continue
        if not line.strip():
            if buf and len('\n'.join(buf)) >= limit * 0.55:
                flush()
            elif buf:
                buf.append(line)
                end = number
            continue
        if buf and len('\n'.join(buf)) + len(line) + 1 > limit:
            flush()
        if not buf:
            start = number
        # Long individual lines are split at sentence/whitespace boundaries without loss.
        remainder = line
        while len(remainder) > limit:
            cut = max(remainder.rfind('. ', 0, limit), remainder.rfind(' ', 0, limit))
            if cut < limit // 2:
                cut = limit
            part, remainder = remainder[:cut].rstrip(), remainder[cut:].lstrip()
            if part:
                buf.append(part)
                end = number
                flush()
                start = number
        if remainder:
            buf.append(remainder)
            end = number
    flush()
    return result


def ingest_file(db: sqlite3.Connection, path: str | Path, storage_root: str | Path,
                source_type: str = 'user_document', title: str | None = None,
                url: str | None = None, published_at: str | None = None) -> IngestResult:
    if source_type not in ALLOWED_TYPES:
        raise IngestionError(f"source_type invalide : {source_type}")
    source_path = Path(path).resolve(strict=True)
    if not source_path.is_file():
        raise IngestionError('La source doit être un fichier régulier.')
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        raise IngestionError(f"Lecture impossible : {exc}") from exc
    normalized = _normalize(raw, source_path)
    digest = sha256_bytes(raw)
    normalized_digest = sha256_bytes(normalized.encode('utf-8'))
    stamp = now()
    doc_title = title or source_path.stem
    identity = f'{source_type}:{source_path.as_posix().casefold()}'
    source_id = str(uuid.uuid5(NAMESPACE, identity))
    document_id = str(uuid.uuid5(NAMESPACE, identity))
    run_id = str(uuid.uuid4())
    root = Path(storage_root)
    original_target = root / 'originals' / digest / _safe_name(source_path.name)

    initialize(db)
    db.execute('''INSERT OR IGNORE INTO sources(source_id,source_type,source_name,original_path,url,created_at)
                  VALUES(?,?,?,?,?,?)''', (source_id, source_type, source_path.name, str(source_path), url, stamp))
    existing_hash = db.execute('SELECT * FROM document_versions WHERE sha256=?', (digest,)).fetchone()
    if existing_hash:
        db.execute('INSERT OR IGNORE INTO document_sources(document_id,source_id) VALUES(?,?)',
                   (existing_hash['document_id'], source_id))
        db.execute('''INSERT INTO ingestion_runs(ingestion_run_id,source_path,source_type,started_at,finished_at,status,
                      document_id,document_version_id,chunks_created,message) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                   (run_id, str(source_path), source_type, stamp, stamp, 'SKIPPED_IDENTICAL',
                    existing_hash['document_id'], existing_hash['document_version_id'], 0,
                    'SHA-256 déjà présent; aucune version ou copie supplémentaire créée.'))
        db.commit()
        return IngestResult('SKIPPED_IDENTICAL', existing_hash['document_id'], existing_hash['version'], digest, 0,
                            doc_title)

    # Acquire the write lock before version selection and keep DB/file publication ordered.
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    try:
        prior = db.execute('SELECT * FROM documents WHERE document_id=?', (document_id,)).fetchone()
        version = (prior['current_version'] + 1) if prior else 1
        if prior and prior['title'] != doc_title:
            db.execute('UPDATE documents SET title=? WHERE document_id=?', (doc_title, document_id))
        else:
            db.execute('INSERT OR IGNORE INTO documents(document_id,title,language,status,created_at,current_version) VALUES(?,?,?,?,?,0)',
                       (document_id, doc_title, 'und', 'active', stamp))
        db.execute('INSERT OR IGNORE INTO document_sources(document_id,source_id) VALUES(?,?)', (document_id, source_id))
        version_id = str(uuid.uuid4())
        normalized_target = root / 'normalized' / document_id / f'v{version}.txt'
        original_target.parent.mkdir(parents=True, exist_ok=True)
        normalized_target.parent.mkdir(parents=True, exist_ok=True)
        # Content-addressed originals are immutable; never replace a different payload.
        if original_target.exists() and sha256_bytes(original_target.read_bytes()) != digest:
            raise IngestionError('Conflit inattendu sur la copie immuable originale.')
        if not original_target.exists():
            shutil.copyfile(source_path, original_target)
        normalized_target.write_text(normalized, encoding='utf-8', newline='\n')
        mime_type = mimetypes.guess_type(source_path.name)[0] or 'text/plain'
        db.execute('''INSERT INTO document_versions(document_version_id,document_id,source_id,version,original_path,
                      original_stored_path,normalized_path,url,published_at,retrieved_at,ingested_at,sha256,mime_type,
                      language,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (version_id, document_id, source_id, version, str(source_path), str(original_target),
                    str(normalized_target), url, published_at, stamp, stamp, digest, mime_type, 'und', 'active'))
        pieces = _chunks(normalized)
        for ordinal, (heading, line_start, line_end, body) in enumerate(pieces, 1):
            db.execute('''INSERT INTO chunks(chunk_id,document_version_id,ordinal,heading,line_start,line_end,text,text_sha256)
                          VALUES(?,?,?,?,?,?,?,?)''',
                       (str(uuid.uuid5(uuid.UUID(version_id), str(ordinal))), version_id, ordinal, heading,
                        line_start, line_end, body, sha256_bytes(body.encode('utf-8'))))
        db.execute('UPDATE documents SET current_version=?,status=? WHERE document_id=?',
                   (version, 'active', document_id))
        db.execute('''INSERT INTO ingestion_runs(ingestion_run_id,source_path,source_type,started_at,finished_at,status,
                      document_id,document_version_id,chunks_created,message) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                   (run_id, str(source_path), source_type, stamp, stamp, 'IMPORTED', document_id, version_id,
                    len(pieces), f'Contenu normalisé SHA-256={normalized_digest}'))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return IngestResult('IMPORTED', document_id, version, digest, len(pieces), doc_title)
