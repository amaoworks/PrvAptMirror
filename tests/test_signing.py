from datetime import datetime, timezone
from pathlib import Path
import subprocess

from prvaptmirror.db import connect, get_setting, init_db
from prvaptmirror.indexer import rebuild_dists
from prvaptmirror.models import PackageRow
from prvaptmirror.signing import ensure_key, sign_release


def test_inrelease_verifies_against_repo_key(ready, tmp_path: Path):
    cfg = ready
    conn = connect(cfg)
    fpr = get_setting(conn, "gpg_fingerprint")
    conn.close()
    assert fpr
    staging = tmp_path / "dists" / cfg.suite
    rebuild_dists([], staging, cfg, now=datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc))
    sign_release(cfg, staging, fpr)
    inrel = staging / "InRelease"
    text = inrel.read_text(encoding="utf-8")
    assert "BEGIN PGP SIGNED MESSAGE" in text
    assert "Origin: PrvAptMirror" in text
    proc = subprocess.run(
        ["gpgv", "--keyring", str(cfg.keyring_path), str(inrel)],
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    release = (staging / "Release").read_bytes()
    # clearsign wraps the original bytes
    assert b"Acquire-By-Hash: no" in release
