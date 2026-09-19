from pathlib import Path
import os
import sqlite3

import pytest

from prvaptmirror.db import connect, get_package, get_package_nva, init_db
from prvaptmirror.debparse import parse_deb
from prvaptmirror.storage import DuplicatePackage, exclusive_link_or_revive, pool_dest
from tests.deb_builder import build_deb
from prvaptmirror.db import get_setting


def _place(cfg, parsed, incoming, conn):
    return exclusive_link_or_revive(
        cfg, conn, parsed, incoming, user_id=None, uploaded_at="2026-08-19T12:00:00Z"
    )


def test_exclusive_create_rejects_live_duplicate(cfg, tmp_path: Path):
    conn = init_db(cfg)
    deb = build_deb(tmp_path / "a.deb", package="dup", version="1.0-1", architecture="amd64")
    incoming1 = tmp_path / "in1.deb"
    incoming1.write_bytes(deb.read_bytes())
    incoming2 = tmp_path / "in2.deb"
    incoming2.write_bytes(deb.read_bytes())
    parsed = parse_deb(deb, allowed_archs=cfg.architectures)
    first = _place(cfg, parsed, incoming1, conn)
    dest = pool_dest(cfg, parsed)
    assert dest.is_file()
    inode = dest.stat().st_ino
    with pytest.raises(DuplicatePackage):
        _place(cfg, parsed, incoming2, conn)
    assert dest.is_file()
    assert dest.stat().st_ino == inode
    row = get_package(conn, first.row_id)
    assert row is not None
    assert row.state == "active"
    conn.close()


def test_missing_same_nva_revives(cfg, tmp_path: Path):
    conn = init_db(cfg)
    deb = build_deb(tmp_path / "b.deb", package="ghost", version="1.0-1", architecture="all")
    incoming1 = tmp_path / "g1.deb"
    incoming1.write_bytes(deb.read_bytes())
    parsed = parse_deb(deb, allowed_archs=cfg.architectures)
    placed = _place(cfg, parsed, incoming1, conn)
    dest = pool_dest(cfg, parsed)
    dest.unlink()
    conn.execute("UPDATE packages SET state='missing' WHERE id=?", (placed.row_id,))
    incoming2 = tmp_path / "g2.deb"
    incoming2.write_bytes(deb.read_bytes())
    revived = _place(cfg, parsed, incoming2, conn)
    assert revived.revived is True
    assert dest.is_file()
    row = get_package_nva(conn, "ghost", "1.0-1", "all")
    assert row is not None
    assert row.state == "active"
    assert row.id == placed.row_id
    conn.close()


@pytest.mark.parametrize("failing_sql", ["INSERT INTO packages", "INSERT INTO settings"])
def test_failed_database_write_rolls_back_blob_and_row(cfg, tmp_path, failing_sql):
    conn = init_db(cfg)
    deb = build_deb(tmp_path / "failure.deb")
    parsed = parse_deb(deb)
    contents = deb.read_bytes()

    class FailingConnection:
        def execute(self, sql, *args):
            if failing_sql in sql:
                raise sqlite3.OperationalError("simulated disk I/O error")
            return conn.execute(sql, *args)

    with pytest.raises(sqlite3.OperationalError):
        _place(cfg, parsed, deb, FailingConnection())
    assert not pool_dest(cfg, parsed).exists()
    assert get_package_nva(conn, parsed.name, parsed.version, parsed.architecture) is None
    assert get_setting(conn, "publish_dirty") == "0"
    deb.write_bytes(contents)
    _place(cfg, parsed, deb, conn)
    assert pool_dest(cfg, parsed).read_bytes() == contents
    assert get_setting(conn, "publish_dirty") == "1"
    conn.close()


def test_retry_adopts_identical_blob_left_by_process_crash(cfg, tmp_path):
    conn = init_db(cfg)
    deb = build_deb(tmp_path / "crash.deb")
    parsed = parse_deb(deb)
    dest = pool_dest(cfg, parsed)
    dest.parent.mkdir(parents=True)
    os.link(deb, dest)  # Crash after link, before inserting the database row.
    inode = dest.stat().st_ino
    placed = _place(cfg, parsed, deb, conn)
    assert dest.stat().st_ino == inode
    assert get_package(conn, placed.row_id).sha256 == parsed.sha256
    assert get_setting(conn, "publish_dirty") == "1"
    conn.close()


def test_retry_never_overwrites_different_orphan_bytes(cfg, tmp_path):
    conn = init_db(cfg)
    deb = build_deb(tmp_path / "conflict.deb")
    parsed = parse_deb(deb)
    dest = pool_dest(cfg, parsed)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"different bytes")
    with pytest.raises(DuplicatePackage):
        _place(cfg, parsed, deb, conn)
    assert dest.read_bytes() == b"different bytes"
    conn.close()


def test_startup_quarantines_orphans_and_keeps_registered_blobs(cfg, tmp_path, monkeypatch):
    from prvaptmirror import publish
    from prvaptmirror.models import PublishResult

    conn = init_db(cfg)
    deb = build_deb(tmp_path / "registered.deb")
    parsed = parse_deb(deb)
    _place(cfg, parsed, deb, conn)
    orphan = cfg.pool_dir / "leftover.deb"
    orphan.write_bytes(b"recoverable crash leftover")
    calls = []
    monkeypatch.setattr(publish, "publish_unlocked", lambda *args: calls.append(True) or PublishResult(ok=True))
    publish.startup_reconcile(cfg, conn)
    assert not orphan.exists()
    assert pool_dest(cfg, parsed).is_file()
    assert next((cfg.data_dir / "quarantine").iterdir()).read_bytes() == b"recoverable crash leftover"
    assert calls == [True]
    conn.close()


def test_quarantine_accepts_a_maximum_length_filename(cfg):
    from prvaptmirror.storage import quarantine_orphaned_blobs
    conn = init_db(cfg)
    orphan = cfg.pool_dir / ("x" * 251 + ".deb")
    orphan.write_bytes(b"keep me")
    assert quarantine_orphaned_blobs(cfg, conn) == 1
    assert next((cfg.data_dir / "quarantine").iterdir()).read_bytes() == b"keep me"
    conn.close()
