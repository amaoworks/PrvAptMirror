from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest


def test_backup_includes_source_encryption_key(tmp_path: Path):
    if shutil.which("sqlite3") is None:
        pytest.skip("sqlite3 CLI is not installed")
    data = tmp_path / "data"
    (data / "repo" / "pool").mkdir(parents=True)
    (data / "gnupg").mkdir()
    (data / "secret-key").write_text("persistent-secret\n", encoding="utf-8")
    conn = sqlite3.connect(data / "data.sqlite")
    conn.executescript(
        """
        CREATE TABLE packages (sha256 TEXT, filename TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO settings(key, value) VALUES ('gpg_fingerprint', 'TEST');
        """
    )
    conn.close()
    destination = tmp_path / "backup"
    env = os.environ.copy()
    env["PRVAPT_DATA_DIR"] = str(data)
    script = Path(__file__).resolve().parents[1] / "scripts" / "backup.sh"
    subprocess.run([str(script), str(destination)], env=env, check=True, capture_output=True)

    with tarfile.open(destination / "pool-and-meta.tgz", "r:gz") as archive:
        names = archive.getnames()
    assert "secret-key" in names
    assert "gnupg" in names
    assert "repo/pool" in names
