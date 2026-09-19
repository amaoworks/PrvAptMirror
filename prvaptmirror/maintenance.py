"""Consistent backups and offline restores. Uses only the Python standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from prvaptmirror.filesystem import file_lock, fsync_directory, rename_exchange, rename_noreplace


def _open_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _check_database(conn: sqlite3.Connection) -> None:
    if [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]:
        raise ValueError("backup database failed integrity_check")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("backup database has invalid foreign keys")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _check_package_files(conn: sqlite3.Connection, data: Path) -> None:
    for row in conn.execute("SELECT filename, sha256, state FROM packages"):
        path = PurePosixPath(row["filename"])
        if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("pool",):
            raise ValueError("invalid package path in backup database")
        blob = data / "repo" / path
        if not blob.is_file():
            if row["state"] == "active":
                raise ValueError(f"backup missing active package: {path}")
        elif _digest(blob) != row["sha256"]:
            raise ValueError(f"backup package checksum mismatch: {path}")


def _sync_tree(root: Path) -> None:
    for directory, _, files in os.walk(root, topdown=False):
        for name in files:
            with (Path(directory) / name).open("rb") as stream:
                os.fsync(stream.fileno())
        fsync_directory(Path(directory))


def backup(data: Path, destination: Path) -> None:
    data, destination = data.resolve(), destination.resolve()
    if destination.exists():
        raise ValueError("backup destination already exists; choose a new directory")
    # Never place the archive inside a tree that is itself being archived.
    for tree in (data / "repo", data / "gnupg"):
        if destination == tree or tree in destination.parents:
            raise ValueError("backup destination must be outside repo and gnupg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".backup-", dir=destination.parent))
    try:
        with file_lock(data / "service.lock", shared=True), file_lock(data / "publish.lock"):
            with closing(_open_database(data / "data.sqlite")) as source:
                with closing(sqlite3.connect(staged / "data.sqlite")) as target:
                    source.backup(target)
                    _check_database(target)
            (staged / "data.sqlite").chmod(0o600)

            def include(member: tarfile.TarInfo):
                if PurePosixPath(member.name).name.startswith("S.") and member.name.startswith("gnupg/"):
                    return None
                if not (member.isfile() or member.isdir()):
                    raise ValueError(f"unsupported backup file: {member.name}")
                return member

            with tarfile.open(staged / "pool-and-meta.tgz", "w:gz") as archive:
                for name in ("repo/pool", "gnupg", "secret-key"):
                    archive.add(data / name, arcname=name, filter=include)
            with closing(_open_database(staged / "data.sqlite")) as snapshot:
                _check_package_files(snapshot, data)
                rows = snapshot.execute("SELECT sha256, filename FROM packages ORDER BY filename").fetchall()
                fingerprint = snapshot.execute("SELECT value FROM settings WHERE key='gpg_fingerprint'").fetchone()
                manifest = [f"packages={len(rows)}", f"fingerprint={fingerprint[0] if fingerprint else ''}"]
                manifest.extend(f"{row['sha256']} {row['filename']}" for row in rows)
                (staged / "manifest.txt").write_text("\n".join(manifest) + "\n")
        # Hashes detect damaged/incomplete backups before touching live data.
        (staged / "checksums.json").write_text(json.dumps({
            name: _digest(staged / name) for name in ("data.sqlite", "pool-and-meta.tgz")
        }) + "\n")
        for path in staged.iterdir():
            path.chmod(0o600)
        _sync_tree(staged)
        rename_noreplace(str(staged), str(destination))
        fsync_directory(destination.parent)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def _extract_archive(source: Path, staged: Path) -> None:
    with tarfile.open(source / "pool-and-meta.tgz", "r:gz") as archive:
        seen: set[str] = set()
        for member in archive:
            path = PurePosixPath(member.name)
            parts = path.parts
            allowed = (
                parts[:2] == ("repo", "pool") or parts[:1] == ("gnupg",)
                or parts == ("secret-key",)
            )
            if path.is_absolute() or ".." in parts or not allowed or member.name in seen:
                raise ValueError(f"unsafe backup path: {member.name}")
            seen.add(member.name)
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"unsupported backup member: {member.name}")
            target = staged.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
            else:
                with archive.extractfile(member) as incoming, target.open("xb") as output:
                    shutil.copyfileobj(incoming, output, length=1024 * 1024)
    for name in ("gnupg", "repo/pool"):
        if not (staged / name).is_dir():
            raise ValueError(f"backup missing {name}")
    if not (staged / "secret-key").is_file() or not (staged / "secret-key").read_bytes().strip():
        raise ValueError("backup missing secret-key")


def restore(source: Path, data: Path, *, uid: int, gid: int) -> Path:
    """Atomically replace offline data; return the retained pre-restore directory."""
    source, data = source.resolve(), data.resolve()
    if source == data:
        raise ValueError("backup and data directories must differ")
    checksums = source / "checksums.json"
    if checksums.exists():
        expected = json.loads(checksums.read_text())
        for name in ("data.sqlite", "pool-and-meta.tgz"):
            if _digest(source / name) != expected.get(name):
                raise ValueError(f"backup checksum mismatch: {name}")
    data.mkdir(parents=True, exist_ok=True)
    # App and backup hold service.lock; refusing a busy lock also protects
    # native deployments and accidental restores into the wrong Compose data.
    with file_lock(data / "service.lock", blocking=False), file_lock(data / "publish.lock", blocking=False):
        staged = Path(tempfile.mkdtemp(prefix=f".{data.name}.before-restore-", dir=data.parent))
        exchanged = False
        try:
            # Copy ONLY the backup database. Old -wal/-shm files are never read
            # or carried over into this fresh directory.
            shutil.copyfile(source / "data.sqlite", staged / "data.sqlite")
            _extract_archive(source, staged)
            with closing(sqlite3.connect(staged / "data.sqlite")) as conn:
                conn.row_factory = sqlite3.Row
                _check_database(conn)
                _check_package_files(conn, staged)
                conn.execute("INSERT INTO settings(key,value) VALUES ('publish_dirty','1') "
                             "ON CONFLICT(key) DO UPDATE SET value='1'")
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
            # Share the lock inodes across the exchange so a concurrently
            # starting process cannot acquire a different service lock.
            for name in ("service.lock", "publish.lock"):
                os.link(data / name, staged / name)
            for directory, _, files in os.walk(staged):
                parent = Path(directory)
                in_repo = parent == staged / "repo" or staged / "repo" in parent.parents
                parent.chmod(0o755 if in_repo else 0o700)
                if os.geteuid() == 0:
                    os.chown(parent, uid, gid)
                elif (parent.stat().st_uid, parent.stat().st_gid) != (uid, gid):
                    raise PermissionError("restore requires the target UID/GID or root")
                for name in files:
                    path = parent / name
                    path.chmod(0o644 if in_repo else 0o600)
                    if os.geteuid() == 0:
                        os.chown(path, uid, gid)
            _sync_tree(staged)
            rename_exchange(str(staged), str(data))
            exchanged = True
            fsync_directory(data.parent)
            return staged
        finally:
            if not exchanged:
                shutil.rmtree(staged)


def _compose_target(env_file: str | None) -> tuple[list[str], Path]:
    command = ["docker", "compose"]
    if env_file:
        command += ["--env-file", env_file]
    result = subprocess.run(command + ["config", "--format", "json"], check=True, capture_output=True, text=True)
    config = json.loads(result.stdout)
    app = config["services"]["app"]
    target = app.get("environment", {}).get("PRVAPT_DATA_DIR", "/var/lib/prvaptmirror")
    mounts = [v for v in app.get("volumes", []) if v.get("target") == target and v.get("type") == "bind"]
    if len(mounts) != 1:
        raise ValueError("restore requires exactly one Compose bind mount for app data")
    return command, Path(mounts[0]["source"]).resolve()


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("backup")
    save.add_argument("destination", type=Path)
    save.add_argument("--data-dir", type=Path, default=Path(os.environ.get("PRVAPT_DATA_DIR", "/var/lib/prvaptmirror")))
    recover = commands.add_parser("restore")
    recover.add_argument("source", type=Path)
    recover.add_argument("--offline", action="store_true", help="native deployment; stop the app before using this option")
    recover.add_argument("--data-dir", type=Path)
    recover.add_argument("--compose-env-file")
    recover.add_argument("--uid", type=int, default=1000)
    recover.add_argument("--gid", type=int, default=1000)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "backup":
            backup(args.data_dir, args.destination)
            print(f"backup written to {args.destination}")
        else:
            if args.offline:
                if args.data_dir is None:
                    raise ValueError("--offline requires an explicit --data-dir")
                data = args.data_dir
            else:
                compose, data = _compose_target(args.compose_env_file)
                if args.data_dir is not None and args.data_dir.resolve() != data:
                    raise ValueError("--data-dir does not match the Compose app data mount")
                subprocess.run(compose + ["stop", "app"], check=True)
                running = subprocess.run(compose + ["ps", "--status", "running", "-q", "app"],
                                         check=True, capture_output=True, text=True)
                if running.stdout.strip():
                    raise RuntimeError("app is still running; refusing to restore")
            previous = restore(args.source, data, uid=args.uid, gid=args.gid)
            print(f"restore complete; previous data retained at {previous}")
            print("start the app; startup will rebuild and sign the repository indexes")
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"maintenance failed: {exc}\n")


if __name__ == "__main__":
    cli()
