from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

from prvaptmirror import maintenance
from prvaptmirror.db import init_db, get_setting
from prvaptmirror.filesystem import file_lock
from prvaptmirror.storage import exclusive_link_or_revive
from prvaptmirror.debparse import parse_deb
from tests.deb_builder import build_deb


def seed(cfg):
    conn = init_db(cfg)
    (cfg.data_dir / "secret-key").write_text(cfg.secret_key + "\n")
    conn.execute("INSERT INTO settings(key,value) VALUES ('gpg_fingerprint','TEST')")
    conn.execute("INSERT INTO publish_runs(started_at,status) VALUES ('2026-09-19','success')")
    conn.close()


def test_backup_includes_source_encryption_key(cfg, tmp_path):
    seed(cfg)
    destination = tmp_path / "backups" / "with spaces"
    env = os.environ.copy()
    env["PRVAPT_DATA_DIR"] = str(cfg.data_dir)
    script = Path(__file__).resolve().parents[1] / "scripts" / "backup.sh"
    subprocess.run([str(script), str(destination)], env=env, check=True, capture_output=True)
    with tarfile.open(destination / "pool-and-meta.tgz", "r:gz") as archive:
        names = archive.getnames()
    assert {"secret-key", "gnupg", "repo/pool"}.issubset(names)
    assert not any(name.startswith("repo/dists") for name in names)
    assert (destination / "data.sqlite").stat().st_mode & 0o777 == 0o600
    assert destination.stat().st_mode & 0o777 == 0o700
    assert json.loads((destination / "checksums.json").read_text())


def test_backup_waits_for_publish_lock_and_captures_one_package_state(cfg, tmp_path, monkeypatch):
    seed(cfg)
    deb = build_deb(tmp_path / "fixture.deb")
    conn = init_db(cfg)
    parsed = parse_deb(deb)
    placement = exclusive_link_or_revive(cfg, conn, parsed, deb, user_id=None, uploaded_at="now")
    destination = tmp_path / "snapshot"
    opened = threading.Event()
    started = threading.Event()
    errors = []
    original = maintenance._open_database

    def track_open(path):
        opened.set()
        return original(path)

    monkeypatch.setattr(maintenance, "_open_database", track_open)

    def save():
        started.set()
        try:
            maintenance.backup(cfg.data_dir, destination)
        except Exception as exc:
            errors.append(exc)

    with file_lock(cfg.lock_path):
        thread = threading.Thread(target=save)
        thread.start()
        assert started.wait(2)
        assert not opened.wait(0.1), "backup read the DB while a publisher held the lock"
        conn.execute("DELETE FROM packages WHERE id=?", (placement.row_id,))
        (cfg.repo_dir / placement.filename).unlink()
    thread.join(10)
    assert not thread.is_alive()
    assert not errors
    conn.close()
    with sqlite3.connect(destination / "data.sqlite") as snapshot:
        assert snapshot.execute("SELECT count(*) FROM packages").fetchone()[0] == 0
    with tarfile.open(destination / "pool-and-meta.tgz") as archive:
        assert not any(member.name.endswith(".deb") for member in archive)


def test_restore_replaces_old_indexes_and_wal_and_preserves_old_directory(cfg, tmp_path):
    seed(cfg)
    source = tmp_path / "snapshot"
    maintenance.backup(cfg.data_dir, source)
    target = tmp_path / "target"
    target.mkdir()
    (target / "repo" / "dists").mkdir(parents=True)
    (target / "repo" / "dists" / "old-index").write_text("stale")
    conn = sqlite3.connect(target / "data.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE target_only (value TEXT)")
    conn.execute("INSERT INTO target_only VALUES ('must not be replayed')")
    conn.commit()
    # Preserve real WAL bytes from a crashed process without leaving a writer.
    wal = (target / "data.sqlite-wal").read_bytes()
    conn.close()
    (target / "data.sqlite-wal").write_bytes(wal)
    (target / "data.sqlite-shm").write_bytes(b"stale shared memory")
    previous = maintenance.restore(source, target, uid=os.getuid(), gid=os.getgid())
    assert (previous / "repo/dists/old-index").read_text() == "stale"
    assert (previous / "data.sqlite-wal").read_bytes() == wal
    assert not (target / "repo/dists").exists()
    assert not (target / "data.sqlite-wal").exists()
    assert not (target / "data.sqlite-shm").exists()
    with sqlite3.connect(target / "data.sqlite") as restored:
        assert restored.execute("SELECT value FROM settings WHERE key='publish_dirty'").fetchone()[0] == "1"
        assert restored.execute("SELECT name FROM sqlite_master WHERE name='target_only'").fetchone() is None
    assert (target / "secret-key").stat().st_mode & 0o777 == 0o600


def test_restore_refuses_a_running_app(cfg, tmp_path):
    seed(cfg)
    source = tmp_path / "snapshot"
    maintenance.backup(cfg.data_dir, source)
    with file_lock(cfg.data_dir / "service.lock", shared=True):
        with pytest.raises(BlockingIOError):
            maintenance.restore(source, cfg.data_dir, uid=os.getuid(), gid=os.getgid())
    assert cfg.db_path.is_file()


def test_restore_aborts_when_compose_stop_fails(cfg, tmp_path, monkeypatch):
    marker = cfg.data_dir / "untouched"
    marker.write_text("original")
    monkeypatch.setattr(maintenance, "_compose_target", lambda env: (["docker", "compose"], cfg.data_dir))

    def fail(command, **kwargs):
        assert command[-2:] == ["stop", "app"]
        raise subprocess.CalledProcessError(42, command)

    monkeypatch.setattr(maintenance.subprocess, "run", fail)
    monkeypatch.setattr("sys.argv", ["maintenance", "restore", str(tmp_path / "backup")])
    with pytest.raises(SystemExit) as error:
        maintenance.cli()
    assert error.value.code == 1
    assert marker.read_text() == "original"
    assert not (cfg.data_dir / "service.lock").exists()


def test_restore_rejects_corrupt_backup_without_touching_target(cfg, tmp_path):
    seed(cfg)
    source = tmp_path / "snapshot"
    maintenance.backup(cfg.data_dir, source)
    (source / "data.sqlite").write_bytes(b"corrupt database")
    with pytest.raises(ValueError, match="checksum"):
        maintenance.restore(source, cfg.data_dir, uid=os.getuid(), gid=os.getgid())
    with sqlite3.connect(cfg.db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_restore_missing_blob_does_not_replace_target(cfg, tmp_path):
    seed(cfg)
    deb = build_deb(tmp_path / "fixture.deb")
    conn = init_db(cfg)
    parsed = parse_deb(deb)
    place = exclusive_link_or_revive(cfg, conn, parsed, deb, user_id=None, uploaded_at="now")
    conn.close()
    source = tmp_path / "incomplete-snapshot"
    maintenance.backup(cfg.data_dir, source)
    (source / "checksums.json").unlink()
    # Simulate an incomplete legacy archive; its database still references the package.
    with tarfile.open(source / "pool-and-meta.tgz", "w:gz") as archive:
        for name in ("gnupg", "secret-key"):
            archive.add(cfg.data_dir / name, arcname=name)
        archive.add(cfg.pool_dir, arcname="repo/pool", recursive=False)
    old_inode = cfg.data_dir.stat().st_ino
    with pytest.raises(ValueError, match="missing active package"):
        maintenance.restore(source, cfg.data_dir, uid=os.getuid(), gid=os.getgid())
    assert cfg.data_dir.stat().st_ino == old_inode


def test_backup_does_not_publish_an_incomplete_snapshot(cfg, tmp_path):
    seed(cfg)
    deb = build_deb(tmp_path / "fixture.deb")
    with init_db(cfg) as conn:
        parsed = parse_deb(deb)
        place = exclusive_link_or_revive(cfg, conn, parsed, deb, user_id=None, uploaded_at="now")
    (cfg.repo_dir / place.filename).unlink()
    destination = tmp_path / "broken-snapshot"
    with pytest.raises(ValueError, match="missing active package"):
        maintenance.backup(cfg.data_dir, destination)
    assert not destination.exists()


def test_backup_never_replaces_a_concurrently_created_destination(cfg, tmp_path, monkeypatch):
    seed(cfg)
    destination = tmp_path / "snapshot"
    original = maintenance.rename_noreplace
    inode = []

    def create_destination_first(source, target):
        Path(target).mkdir()
        inode.append(Path(target).stat().st_ino)
        original(source, target)

    monkeypatch.setattr(maintenance, "rename_noreplace", create_destination_first)
    with pytest.raises(FileExistsError):
        maintenance.backup(cfg.data_dir, destination)
    assert destination.stat().st_ino == inode[0]
    assert not list(destination.iterdir())


def test_restore_rejects_archive_path_traversal(cfg, tmp_path):
    import io
    seed(cfg)
    source = tmp_path / "snapshot"
    maintenance.backup(cfg.data_dir, source)
    (source / "checksums.json").unlink()  # Legacy backups have no checksum file.
    with tarfile.open(source / "pool-and-meta.tgz", "w:gz") as archive:
        member = tarfile.TarInfo("../../outside")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"evil"))
    with pytest.raises(ValueError, match="unsafe backup path"):
        maintenance.restore(source, cfg.data_dir, uid=os.getuid(), gid=os.getgid())
    assert not (tmp_path.parent / "outside").exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to simulate host maintenance")
@pytest.mark.parametrize("operation", ["backup", "failed-restore"])
def test_root_maintenance_keeps_locks_accessible_to_container_user(cfg, tmp_path, operation):
    seed(cfg)
    source = tmp_path / "snapshot"
    maintenance.backup(cfg.data_dir, source)
    for name in ("service.lock", "publish.lock"):
        (cfg.data_dir / name).unlink()
    try:
        os.chown(cfg.data_dir, 1000, 1000)
    except PermissionError:
        pytest.skip("user namespace does not permit UID 1000 ownership")
    if operation == "backup":
        maintenance.backup(cfg.data_dir, tmp_path / "second-snapshot")
    else:
        (source / "checksums.json").unlink()
        (source / "pool-and-meta.tgz").write_bytes(b"invalid tar")
        with pytest.raises(tarfile.TarError):
            maintenance.restore(source, cfg.data_dir, uid=1000, gid=1000)
    for name in ("service.lock", "publish.lock"):
        st = (cfg.data_dir / name).stat()
        assert (st.st_uid, st.st_gid, st.st_mode & 0o777) == (1000, 1000, 0o600)
