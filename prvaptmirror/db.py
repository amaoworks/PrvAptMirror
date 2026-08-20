"""SQLite schema, migrations, helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from prvaptmirror.config import Config
from prvaptmirror.models import PackageRow, User

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  must_change_password INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  last_login_at TEXT
);

CREATE TABLE sessions (
  id           TEXT PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  ip           TEXT,
  user_agent   TEXT
);

CREATE TABLE packages (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  version       TEXT NOT NULL,
  architecture  TEXT NOT NULL,
  component     TEXT NOT NULL DEFAULT 'main',
  filename      TEXT NOT NULL UNIQUE,
  size          INTEGER NOT NULL,
  md5           TEXT NOT NULL,
  sha1          TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  control_json  TEXT NOT NULL,
  state         TEXT NOT NULL DEFAULT 'active',
  uploaded_at   TEXT NOT NULL,
  uploaded_by   INTEGER REFERENCES users(id),
  UNIQUE(name, version, architecture)
);

CREATE INDEX idx_packages_name ON packages(name);
CREATE INDEX idx_packages_state ON packages(state);

CREATE TABLE publish_runs (
  id            INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL,
  duration_ms   INTEGER,
  package_count INTEGER,
  error         TEXT
);

CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE login_attempts (
  id       INTEGER PRIMARY KEY,
  ip       TEXT NOT NULL,
  username TEXT,
  success  INTEGER NOT NULL,
  at       TEXT NOT NULL
);

CREATE INDEX idx_login_ip_at ON login_attempts(ip, at);
INSERT INTO settings(key, value) VALUES ('publish_dirty', '0');
"""
}


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cfg.db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in range(current + 1, SCHEMA_VERSION + 1):
        conn.executescript(MIGRATIONS[version])
        conn.execute(f"PRAGMA user_version = {version}")


def init_db(cfg: Config) -> sqlite3.Connection:
    conn = connect(cfg)
    migrate(conn)
    try:
        cfg.db_path.chmod(0o600)
    except OSError:
        pass
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return str(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def user_from_row(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        must_change_password=bool(row["must_change_password"]),
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


def get_user_by_username(conn: sqlite3.Connection, username: str) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
    return user_from_row(row) if row else None


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return user_from_row(row) if row else None


def list_packages(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    arch: str | None = None,
    state: str | None = None,
) -> list[PackageRow]:
    sql = "SELECT * FROM packages WHERE 1=1"
    params: list[object] = []
    if q:
        sql += " AND (name LIKE ? OR version LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like])
    if arch:
        sql += " AND architecture = ?"
        params.append(arch)
    if state:
        sql += " AND state = ?"
        params.append(state)
    sql += " ORDER BY name COLLATE NOCASE, version, architecture"
    return [PackageRow.from_row(r) for r in conn.execute(sql, params)]


def get_package(conn: sqlite3.Connection, package_id: int) -> PackageRow | None:
    row = conn.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()
    return PackageRow.from_row(row) if row else None


def get_package_nva(
    conn: sqlite3.Connection, name: str, version: str, architecture: str
) -> PackageRow | None:
    row = conn.execute(
        "SELECT * FROM packages WHERE name = ? AND version = ? AND architecture = ?",
        (name, version, architecture),
    ).fetchone()
    return PackageRow.from_row(row) if row else None


def last_publish_status(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT status FROM publish_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else str(row["status"])


def last_publish_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM publish_runs ORDER BY id DESC LIMIT 1").fetchone()


def db_exists(path: Path) -> bool:
    return path.is_file()
