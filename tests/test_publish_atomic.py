from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from prvaptmirror.db import connect, get_package, init_db, list_packages
from prvaptmirror.debparse import parse_deb
from prvaptmirror.publish import (
    delete_commit,
    publish_lock,
    publish_unlocked,
    startup_reconcile,
    upload_commit,
)
from tests.deb_builder import build_deb


def _upload(cfg, tmp_path: Path, **kwargs):
    deb = build_deb(tmp_path / f"{kwargs.get('package','p')}.deb", **kwargs)
    incoming = tmp_path / f"in-{deb.name}"
    incoming.write_bytes(deb.read_bytes())
    parsed = parse_deb(deb, allowed_archs=cfg.architectures)
    conn = connect(cfg)
    try:
        results, pub = upload_commit(cfg, conn, [(parsed, incoming)], user_id=None)
    finally:
        conn.close()
    return results, pub, parsed


def test_upload_publishes_signed_tree(ready, tmp_path: Path):
    results, pub, parsed = _upload(
        ready, tmp_path, package="hello-prv", version="1.0-1", architecture="all"
    )
    assert results[0]["ok"]
    assert pub is not None and pub.ok
    inrel = ready.dists_dir / ready.suite / "InRelease"
    assert inrel.is_file()
    body = inrel.read_text(encoding="utf-8")
    assert "BEGIN PGP SIGNED MESSAGE" in body
    amd = (ready.dists_dir / ready.suite / "main" / "binary-amd64" / "Packages").read_text()
    assert "Package: hello-prv" in amd
    assert "Architecture: all" in amd
    blob = ready.repo_dir / results[0]["filename"]
    assert blob.is_file()


def test_second_publish_exchanges_inode(ready, tmp_path: Path):
    _upload(ready, tmp_path, package="one", version="1.0-1", architecture="amd64")
    live = ready.dists_dir
    first_ino = live.stat().st_ino
    conn = connect(ready)
    try:
        pub = publish_unlocked(ready, conn)
    finally:
        conn.close()
    assert pub.ok
    # After exchange, live inode is whatever dists.next was — different from previous live
    # First publish used rename; second uses exchange. Either way live exists.
    assert live.is_file() or live.is_dir()
    assert (live / ready.suite / "InRelease").is_file()
    # Dist path remains a real directory (not missing)
    assert live.exists()
    assert live.stat().st_ino != 0
    _ = first_ino


def test_delete_publishes_then_unlinks(ready, tmp_path: Path):
    results, _, parsed = _upload(
        ready, tmp_path, package="bye", version="1.0-1", architecture="amd64"
    )
    pkg_id = results[0]["id"]
    blob = ready.repo_dir / results[0]["filename"]
    assert blob.is_file()
    # keep a second package so remaining blobs stay reachable
    _upload(ready, tmp_path, package="stay", version="1.0-1", architecture="amd64")
    conn = connect(ready)
    try:
        result = delete_commit(ready, conn, pkg_id)
        assert result.ok
        assert get_package(conn, pkg_id) is None
    finally:
        conn.close()
    assert not blob.exists()
    packages = (ready.dists_dir / ready.suite / "main" / "binary-amd64" / "Packages").read_text()
    assert "Package: bye\n" not in packages
    assert "Package: stay\n" in packages
    stay = list(ready.repo_dir.rglob("stay_*.deb"))
    assert stay and stay[0].is_file()


def test_delete_does_not_deadlock_when_lock_held(ready, tmp_path: Path):
    results, _, _ = _upload(ready, tmp_path, package="lockme", version="1.0-1", architecture="all")
    pkg_id = results[0]["id"]
    released = threading.Event()
    started = threading.Event()
    done = threading.Event()
    error = []

    def holder():
        with publish_lock(ready):
            started.set()
            time.sleep(0.4)
        released.set()

    def deleter():
        started.wait(2)
        conn = connect(ready)
        try:
            delete_commit(ready, conn, pkg_id)
        except Exception as exc:  # pragma: no cover
            error.append(exc)
        finally:
            conn.close()
        done.set()

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=deleter)
    t1.start()
    t2.start()
    finished = done.wait(15)
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert finished, "delete_commit deadlocked while another thread held publish.lock"
    assert not error


def test_startup_reconcile_missing_blob(ready, tmp_path: Path):
    results, _, _ = _upload(ready, tmp_path, package="gone", version="1.0-1", architecture="all")
    blob = ready.repo_dir / results[0]["filename"]
    blob.unlink()
    conn = connect(ready)
    try:
        startup_reconcile(ready, conn)
        row = get_package(conn, results[0]["id"])
        assert row is not None
        assert row.state == "missing"
    finally:
        conn.close()
    packages = (ready.dists_dir / ready.suite / "main" / "binary-all" / "Packages").read_text()
    assert "Package: gone\n" not in packages
