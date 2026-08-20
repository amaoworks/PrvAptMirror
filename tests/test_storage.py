from pathlib import Path

import pytest

from prvaptmirror.db import connect, get_package, get_package_nva, init_db
from prvaptmirror.debparse import parse_deb
from prvaptmirror.storage import DuplicatePackage, exclusive_link_or_revive, pool_dest
from tests.deb_builder import build_deb


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
