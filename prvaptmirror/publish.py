"""Atomic publish / upload_commit / delete_commit. Lock is never taken in async code."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import os
import shutil
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from prvaptmirror.config import Config
from prvaptmirror.db import (
    get_package,
    get_setting,
    last_publish_status,
    list_packages,
    set_setting,
)
from prvaptmirror.debparse import ParsedDeb
from prvaptmirror.events import emit
from prvaptmirror.indexer import rebuild_dists
from prvaptmirror.models import PackageRow, PublishResult
from prvaptmirror.signing import SigningError, sign_release
from prvaptmirror.storage import (
    DuplicatePackage,
    exclusive_link_or_revive,
    prune_empty_parents,
)

AT_FDCWD = -100
RENAME_EXCHANGE = 2

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.renameat2.restype = ctypes.c_int
_libc.renameat2.argtypes = [
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
]


class PublishError(RuntimeError):
    pass


def rename_exchange(a: str, b: str) -> None:
    rc = _libc.renameat2(
        AT_FDCWD,
        a.encode("utf-8"),
        AT_FDCWD,
        b.encode("utf-8"),
        RENAME_EXCHANGE,
    )
    if rc != 0:
        err = ctypes.get_errno()
        if err == errno.ENOSYS:
            raise OSError(
                err,
                "renameat2(RENAME_EXCHANGE) unsupported; refusing two-step mv",
                a,
                None,
                b,
            )
        raise OSError(err, os.strerror(err), a, None, b)


@contextmanager
def publish_lock(cfg: Config):
    """Synchronous code only. Never enter from async def / the event-loop thread."""
    fd = os.open(str(cfg.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fsync_tree_files(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            with path.open("rb") as fh:
                os.fsync(fh.fileno())
        dir_fd = os.open(dirpath, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _chmod_repo_tree(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            os.chmod(Path(dirpath) / name, 0o644)


def _quarantine_missing(cfg: Config, conn) -> int:
    n = 0
    for row in list_packages(conn, state="active"):
        blob = cfg.repo_dir / row.filename
        if not blob.is_file():
            conn.execute("UPDATE packages SET state = 'missing' WHERE id = ?", (row.id,))
            emit("package_missing", package=row.name, version=row.version, arch=row.architecture)
            n += 1
    if n:
        set_setting(conn, "publish_dirty", "1")
    return n


def publish_unlocked(cfg: Config, conn, *, now: datetime | None = None) -> PublishResult:
    """Assumes publish.lock is already held. Must not open the lock file."""
    started = time.monotonic()
    started_at = _now_iso()
    conn.execute(
        "INSERT INTO publish_runs (started_at, status) VALUES (?, 'running')",
        (started_at,),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    try:
        fingerprint = get_setting(conn, "gpg_fingerprint")
        if not fingerprint:
            raise PublishError("gpg fingerprint missing; refuse to publish")
        _quarantine_missing(cfg, conn)
        rows = [r for r in list_packages(conn) if r.state == "active"]
        present: list[PackageRow] = []
        for row in rows:
            if (cfg.repo_dir / row.filename).is_file():
                present.append(row)
            else:
                conn.execute("UPDATE packages SET state = 'missing' WHERE id = ?", (row.id,))
        staging_root = cfg.staging_dir / uuid.uuid4().hex
        staging_suite = staging_root / "dists" / cfg.suite
        staging_suite.mkdir(parents=True, exist_ok=True)
        skipped = rebuild_dists(present, staging_suite, cfg, now=now)
        sign_release(cfg, staging_suite, fingerprint)
        _fsync_tree_files(staging_root / "dists")
        dists_next = cfg.repo_dir / "dists.next"
        if dists_next.exists():
            shutil.rmtree(dists_next)
        shutil.move(str(staging_root / "dists"), str(dists_next))
        _chmod_repo_tree(dists_next)
        live = cfg.dists_dir
        if not live.exists():
            os.rename(str(dists_next), str(live))
        else:
            before_next = dists_next.stat().st_ino
            rename_exchange(str(dists_next), str(live))
            after_live = live.stat().st_ino
            if after_live != before_next:
                raise PublishError("renameat2 exchange did not move dists.next into place")
            leftover = cfg.repo_dir / "dists.next"
            if leftover.exists():
                shutil.rmtree(leftover)
        shutil.rmtree(staging_root, ignore_errors=True)
        duration_ms = int((time.monotonic() - started) * 1000)
        conn.execute(
            """
            UPDATE publish_runs SET finished_at = ?, status = 'success',
              duration_ms = ?, package_count = ?, error = NULL
            WHERE id = ?
            """,
            (_now_iso(), duration_ms, len(present), run_id),
        )
        set_setting(conn, "publish_dirty", "0")
        emit("publish_ok", duration_ms=duration_ms, package_count=len(present))
        live_inode = cfg.dists_dir.stat().st_ino if cfg.dists_dir.exists() else None
        return PublishResult(
            ok=True,
            skipped=skipped,
            package_count=len(present),
            duration_ms=duration_ms,
            dists_inode=live_inode,
        )
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        conn.execute(
            """
            UPDATE publish_runs SET finished_at = ?, status = 'failed',
              duration_ms = ?, error = ?
            WHERE id = ?
            """,
            (_now_iso(), duration_ms, str(exc), run_id),
        )
        set_setting(conn, "publish_dirty", "1")
        emit("publish_fail", error=str(exc))
        if isinstance(exc, (PublishError, SigningError, OSError)):
            return PublishResult(ok=False, error=str(exc), duration_ms=duration_ms)
        return PublishResult(ok=False, error=str(exc), duration_ms=duration_ms)


def publish(cfg: Config, conn, *, now: datetime | None = None) -> PublishResult:
    with publish_lock(cfg):
        return publish_unlocked(cfg, conn, now=now)


def upload_commit(
    cfg: Config,
    conn,
    items: list[tuple[ParsedDeb, Path]],
    *,
    user_id: int | None,
) -> tuple[list[dict], PublishResult | None]:
    """Place every item under one flock, then publish_unlocked once."""
    results: list[dict] = []
    with publish_lock(cfg):
        placed = 0
        for parsed, incoming in items:
            try:
                place = exclusive_link_or_revive(
                    cfg,
                    conn,
                    parsed,
                    incoming,
                    user_id=user_id,
                    uploaded_at=_now_iso(),
                )
                results.append(
                    {
                        "ok": True,
                        "id": place.row_id,
                        "revived": place.revived,
                        "name": parsed.name,
                        "version": parsed.version,
                        "architecture": parsed.architecture,
                        "filename": place.filename,
                    }
                )
                placed += 1
                emit(
                    "upload_ok",
                    package=parsed.name,
                    version=parsed.version,
                    arch=parsed.architecture,
                    revived=place.revived,
                )
            except DuplicatePackage as exc:
                results.append(
                    {
                        "ok": False,
                        "error": "duplicate",
                        "name": exc.name,
                        "version": exc.version,
                        "architecture": exc.architecture,
                    }
                )
                emit(
                    "upload_reject",
                    reason="duplicate",
                    package=exc.name,
                    version=exc.version,
                    arch=exc.architecture,
                )
        if placed:
            set_setting(conn, "publish_dirty", "1")
            pub = publish_unlocked(cfg, conn)
            return results, pub
        return results, None


def _unlink_blob(cfg: Config, filename: str) -> None:
    blob = cfg.repo_dir / filename
    blob.unlink(missing_ok=True)
    prune_empty_parents(blob.parent, cfg.pool_dir)


def delete_commit(cfg: Config, conn, package_id: int) -> PublishResult:
    with publish_lock(cfg):
        row = get_package(conn, package_id)
        if row is None:
            return PublishResult(ok=False, error="not found")
        conn.execute(
            "UPDATE packages SET state = 'pending_delete' WHERE id = ?",
            (package_id,),
        )
        set_setting(conn, "publish_dirty", "1")
        result = publish_unlocked(cfg, conn)
        if result.ok:
            _unlink_blob(cfg, row.filename)
            conn.execute("DELETE FROM packages WHERE id = ?", (package_id,))
            set_setting(conn, "publish_dirty", "0")
            emit("delete", package=row.name, version=row.version, arch=row.architecture)
        return result


def _finish_orphaned_pending_deletes(cfg: Config, conn) -> None:
    if last_publish_status(conn) != "success":
        return
    if get_setting(conn, "publish_dirty", "0") == "1":
        return
    for row in list_packages(conn, state="pending_delete"):
        _unlink_blob(cfg, row.filename)
        conn.execute("DELETE FROM packages WHERE id = ?", (row.id,))


def startup_reconcile(cfg: Config, conn) -> None:
    with publish_lock(cfg):
        live = cfg.dists_dir
        nxt = cfg.repo_dir / "dists.next"
        last = last_publish_status(conn)
        if live.exists() and nxt.exists():
            if last != "success":
                shutil.rmtree(nxt, ignore_errors=True)
            else:
                shutil.rmtree(nxt, ignore_errors=True)
        elif nxt.exists() and not live.exists():
            shutil.rmtree(nxt, ignore_errors=True)
        _quarantine_missing(cfg, conn)
        _finish_orphaned_pending_deletes(cfg, conn)
        dirty = get_setting(conn, "publish_dirty", "0") == "1"
        if (not live.exists()) or dirty or last != "success":
            publish_unlocked(cfg, conn)
