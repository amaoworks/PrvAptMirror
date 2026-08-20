"""Incoming writes, exclusive pool placement, disk preflight."""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from prvaptmirror.config import Config
from prvaptmirror.debparse import ParsedDeb
from prvaptmirror.db import get_package_nva


class DuplicatePackage(Exception):
    def __init__(self, name: str, version: str, architecture: str) -> None:
        super().__init__(f"{name} {version} {architecture} already exists")
        self.name = name
        self.version = version
        self.architecture = architecture


class DiskFullError(Exception):
    pass


def pool_prefix(name: str) -> str:
    if name.startswith("lib") and len(name) >= 4:
        return name[:4]
    return name[0]


def pool_relative(parsed: ParsedDeb, component: str) -> str:
    prefix = pool_prefix(parsed.name)
    filename = f"{parsed.name}_{parsed.version}_{parsed.architecture}.deb"
    return f"pool/{component}/{prefix}/{parsed.name}/{filename}"


def pool_dest(cfg: Config, parsed: ParsedDeb) -> Path:
    return cfg.repo_dir / pool_relative(parsed, cfg.component)


def disk_preflight(path: Path, needed: int, reserve: int = 1024 * 1024 * 1024) -> None:
    st = os.statvfs(path)
    free = st.f_bavail * st.f_frsize
    if free < needed + reserve:
        raise DiskFullError(
            f"disk free {free} bytes; need {needed}+{reserve}"
        )


def write_incoming(cfg: Config, data: bytes, *, limit: int) -> Path:
    if len(data) > limit:
        raise ValueError("upload exceeds PRVAPT_MAX_UPLOAD_MB")
    disk_preflight(cfg.incoming_dir, len(data))
    dest = cfg.incoming_dir / f"{uuid.uuid4().hex}.deb"
    fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        written = 0
        view = memoryview(data)
        while written < len(data):
            n = os.write(fd, view[written : written + 1024 * 1024])
            written += n
            if written > limit:
                os.close(fd)
                dest.unlink(missing_ok=True)
                raise ValueError("upload exceeds PRVAPT_MAX_UPLOAD_MB")
        os.fsync(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return dest


def write_incoming_stream(cfg: Config, chunks, *, limit: int) -> Path:
    disk_preflight(cfg.incoming_dir, min(limit, 8 * 1024 * 1024))
    dest = cfg.incoming_dir / f"{uuid.uuid4().hex}.deb"
    fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    written = 0
    try:
        for chunk in chunks:
            if not chunk:
                continue
            written += len(chunk)
            if written > limit:
                os.close(fd)
                dest.unlink(missing_ok=True)
                raise ValueError("upload exceeds PRVAPT_MAX_UPLOAD_MB")
            os.write(fd, chunk)
        os.fsync(fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        dest.unlink(missing_ok=True)
        raise
    os.close(fd)
    return dest


def gc_incoming(cfg: Config, max_age_s: int = 24 * 3600) -> None:
    now = time.time()
    if not cfg.incoming_dir.is_dir():
        return
    for path in cfg.incoming_dir.iterdir():
        try:
            if now - path.stat().st_mtime > max_age_s:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def prune_empty_parents(start: Path, stop: Path) -> None:
    current = start.resolve()
    stop = stop.resolve()
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


@dataclass
class PlaceResult:
    row_id: int
    revived: bool
    filename: str


def exclusive_link_or_revive(
    cfg: Config,
    conn,
    parsed: ParsedDeb,
    incoming_path: Path,
    *,
    user_id: int | None,
    uploaded_at: str,
) -> PlaceResult:
    """Must be called while holding publish.lock. incoming and dest share a filesystem."""
    dest = pool_dest(cfg, parsed)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o755)
    try:
        os.link(str(incoming_path), str(dest))
    except FileExistsError as exc:
        incoming_path.unlink(missing_ok=True)
        raise DuplicatePackage(parsed.name, parsed.version, parsed.architecture) from exc
    os.chmod(dest, 0o644)
    st = dest.stat()
    filename = dest.relative_to(cfg.repo_dir).as_posix()
    control_json = parsed.control_json()
    try:
        cur = conn.execute(
            """
            INSERT INTO packages (
              name, version, architecture, component, filename, size,
              md5, sha1, sha256, control_json, state, uploaded_at, uploaded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                parsed.name,
                parsed.version,
                parsed.architecture,
                cfg.component,
                filename,
                parsed.size,
                parsed.md5,
                parsed.sha1,
                parsed.sha256,
                control_json,
                uploaded_at,
                user_id,
            ),
        )
        return PlaceResult(row_id=int(cur.lastrowid), revived=False, filename=filename)
    except sqlite3.IntegrityError as exc:
        # sqlite3.IntegrityError — revive ghost/missing row; never unlink the new inode
        existing = get_package_nva(conn, parsed.name, parsed.version, parsed.architecture)
        if existing is None:
            try:
                if dest.exists() and dest.stat().st_ino == st.st_ino:
                    dest.unlink()
            except OSError:
                pass
            raise DuplicatePackage(parsed.name, parsed.version, parsed.architecture) from exc
        conn.execute(
            """
            UPDATE packages SET
              filename = ?, size = ?, md5 = ?, sha1 = ?, sha256 = ?,
              control_json = ?, state = 'active', uploaded_at = ?, uploaded_by = ?
            WHERE id = ?
            """,
            (
                filename,
                parsed.size,
                parsed.md5,
                parsed.sha1,
                parsed.sha256,
                control_json,
                uploaded_at,
                user_id,
                existing.id,
            ),
        )
        return PlaceResult(row_id=existing.id, revived=True, filename=filename)
    finally:
        incoming_path.unlink(missing_ok=True)
