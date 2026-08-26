"""Regression tests for the GCS <-> local DuckDB sync guards.

Both cases below caused a real prod outage on 2026-08-25/26 (renders
stopped for two days):
  - a stale .wal from a killed run corrupted the freshly pulled DB
  - the broken DB was then pushed back over the authoritative copy

Run: python -m unittest tools/robotics-ingest/test_duckdb_sync.py -v
(needs duckdb; run inside the ingest image.)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import duckdb  # noqa: E402

from src import duckdb_sync  # noqa: E402


def _make_db(path: str) -> None:
    con = duckdb.connect(path)
    con.execute("CREATE TABLE catalysts (entry_id VARCHAR)")
    con.execute("CREATE TABLE entities (name VARCHAR)")
    con.execute("INSERT INTO catalysts VALUES ('ROB-010126-001')")
    con.close()


class TestPullClearsStaleSidecars(unittest.TestCase):
    def test_stale_wal_removed_before_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "robotics.duckdb")
            remote = os.path.join(tmp, "remote.duckdb")
            _make_db(remote)
            # Simulate a warm instance: previous run left a .wal behind.
            wal = Path(f"{db_path}.wal")
            wal.write_bytes(b"stale wal from a killed run")

            fake_blob = mock.MagicMock()
            fake_blob.exists.return_value = True
            fake_blob.download_to_filename.side_effect = (
                lambda dest: Path(dest).write_bytes(Path(remote).read_bytes()))
            fake_storage = mock.MagicMock()
            fake_storage.Client.return_value.bucket.return_value.blob.return_value = fake_blob

            with mock.patch.dict(os.environ, {"DUCKDB_GCS_BUCKET": "test-bucket"}), \
                    mock.patch.dict(sys.modules, {"google.cloud.storage": fake_storage}), \
                    mock.patch("google.cloud.storage", fake_storage, create=True):
                out = duckdb_sync.pull(db_path)

            self.assertTrue(out["pulled"])
            self.assertFalse(wal.exists(), "stale .wal must be deleted before download")
            # The pulled DB must open cleanly (a replayed stale WAL would not).
            con = duckdb.connect(db_path, read_only=True)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM catalysts").fetchone()[0], 1)
            con.close()


class TestPushIntegrityGuard(unittest.TestCase):
    def test_refuses_to_upload_corrupt_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "robotics.duckdb")
            Path(db_path).write_bytes(b"not a duckdb file at all")
            with mock.patch.dict(os.environ, {"DUCKDB_GCS_BUCKET": "test-bucket"}):
                out = duckdb_sync.push(db_path)
            self.assertFalse(out["pushed"])
            self.assertEqual(out["reason"], "integrity_probe_failed")

    def test_uploads_healthy_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "robotics.duckdb")
            _make_db(db_path)
            fake_blob = mock.MagicMock()
            fake_storage = mock.MagicMock()
            fake_storage.Client.return_value.bucket.return_value.blob.return_value = fake_blob
            with mock.patch.dict(os.environ, {"DUCKDB_GCS_BUCKET": "test-bucket"}), \
                    mock.patch.dict(sys.modules, {"google.cloud.storage": fake_storage}), \
                    mock.patch("google.cloud.storage", fake_storage, create=True):
                out = duckdb_sync.push(db_path)
            self.assertTrue(out["pushed"])
            fake_blob.upload_from_filename.assert_called_once()


if __name__ == "__main__":
    unittest.main()
