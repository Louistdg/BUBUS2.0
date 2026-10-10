from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from invest_ai.knowledge.ingest import IngestionError, ingest_file
from invest_ai.knowledge.search import search
from invest_ai.storage.database import connect, fts5_available, initialize

REPORTS = [
    ('CODEX-A', 'ARCHITECTURE-01.md'),
    ('CODEX-B', 'DATA-01.md'),
    ('CODEX-C', 'TRADING-ENGINE-01.md'),
    ('CODEX-D', 'RISK-AUDIT-01.md'),
    ('CODEX-E', 'LLM-RESEARCH-01.md'),
]


class KnowledgeCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp.name)
        self.storage = self.temp_path / 'knowledge'
        self.db = connect(self.temp_path / 'knowledge.sqlite3')
        initialize(self.db)
        self.report_paths = [ROOT / 'handoffs' / folder / name for folder, name in REPORTS]
        self.report_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.report_paths}

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def test_01_initialization_and_fts5(self) -> None:
        initialize(self.db)
        self.assertTrue(fts5_available(self.db))
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'sources','documents','document_versions','chunks','ingestion_runs','index_versions'} <= tables)

    def test_02_ingest_text_document(self) -> None:
        fixture = self.temp_path / 'sample.md'
        fixture.write_text('# Alpha\n\nA first paragraph with searchable provenance.\n', encoding='utf-8')
        result = ingest_file(self.db, fixture, self.storage)
        self.assertEqual(result.status, 'IMPORTED')
        self.assertEqual(result.version, 1)
        self.assertGreater(result.chunks, 0)
        self.assertTrue((self.storage / 'originals' / result.sha256 / 'sample.md').exists())

    def test_03_reingest_identical_is_idempotent(self) -> None:
        fixture = self.temp_path / 'same.txt'
        fixture.write_text('stable content', encoding='utf-8')
        first = ingest_file(self.db, fixture, self.storage)
        second = ingest_file(self.db, fixture, self.storage)
        self.assertEqual(first.status, 'IMPORTED')
        self.assertEqual(second.status, 'SKIPPED_IDENTICAL')
        self.assertEqual(self.db.execute('SELECT count(*) FROM document_versions').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM chunks').fetchone()[0], first.chunks)

    def test_04_changed_fixture_creates_new_version(self) -> None:
        fixture = self.temp_path / 'changing.md'
        fixture.write_text('# Start\nold value', encoding='utf-8')
        first = ingest_file(self.db, fixture, self.storage)
        fixture.write_text('# Start\nnew value', encoding='utf-8')
        second = ingest_file(self.db, fixture, self.storage)
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertEqual(second.version, 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM document_versions').fetchone()[0], 2)
        self.assertTrue((self.storage / 'normalized' / first.document_id / 'v1.txt').exists())
        self.assertTrue((self.storage / 'normalized' / first.document_id / 'v2.txt').exists())

    def test_05_sha256_matches_source_bytes(self) -> None:
        fixture = self.temp_path / 'hash.txt'
        payload = b'Hash the exact original bytes.\r\n'
        fixture.write_bytes(payload)
        result = ingest_file(self.db, fixture, self.storage)
        self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())
        stored = self.db.execute('SELECT sha256 FROM document_versions').fetchone()[0]
        self.assertEqual(stored, result.sha256)

    def test_06_chunk_line_and_heading_provenance(self) -> None:
        fixture = self.temp_path / 'sections.md'
        fixture.write_text('# Risk\n\nLine one.\nLine two.\n', encoding='utf-8')
        result = ingest_file(self.db, fixture, self.storage)
        chunk = self.db.execute('SELECT * FROM chunks WHERE document_version_id=(SELECT document_version_id FROM document_versions WHERE document_id=?)',
                                (result.document_id,)).fetchone()
        self.assertEqual(chunk['heading'], 'Risk')
        self.assertEqual((chunk['line_start'], chunk['line_end']), (1, 4))
        self.assertEqual(len(chunk['text_sha256']), 64)

    def _import_reports(self) -> None:
        for path in self.report_paths:
            ingest_file(self.db, path, self.storage, source_type='codex_report')

    def test_07_lean_search_returns_codex_c(self) -> None:
        self._import_reports()
        rows = search(self.db, 'LEAN')
        self.assertTrue(any('TRADING-ENGINE-01' in row['title'] for row in rows))

    def test_08_evidence_gate_phrase_returns_codex_e(self) -> None:
        self._import_reports()
        rows = search(self.db, '"Evidence Gate"')
        self.assertTrue(any('LLM-RESEARCH-01' in row['title'] for row in rows))

    def test_09_risk_engine_search_returns_codex_d(self) -> None:
        self._import_reports()
        rows = search(self.db, '"Risk Engine"')
        self.assertTrue(any('RISK-AUDIT-01' in row['title'] for row in rows))

    def test_10_invalid_format_fails_explicitly_and_is_not_copied(self) -> None:
        fixture = self.temp_path / 'invalid.pdf'
        fixture.write_bytes(b'%PDF placeholder')
        with self.assertRaisesRegex(IngestionError, 'Format non pris en charge'):
            ingest_file(self.db, fixture, self.storage)
        self.assertEqual(self.db.execute('SELECT count(*) FROM document_versions').fetchone()[0], 0)
        self.assertEqual(fixture.read_bytes(), b'%PDF placeholder')

    def test_11_source_reports_remain_byte_identical(self) -> None:
        self._import_reports()
        for path, expected in self.report_hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected, str(path))
        self.assertEqual(self.db.execute('SELECT count(*) FROM document_versions').fetchone()[0], 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
