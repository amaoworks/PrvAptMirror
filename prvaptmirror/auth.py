"""Password login, hashed sessions, bootstrap admin."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from prvaptmirror.config import Config
from prvaptmirror.db import get_user_by_id, get_user_by_username, user_from_row
from prvaptmirror.events import emit
from prvaptmirror.models import User
from prvaptmirror.ratelimit import iso

HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
SESSION_COOKIE = "prvapt_session"
DUMMY_PASSWORD = "not-the-real-password-used-only-for-timing"
_dummy_hash: str | None = None


def dummy_hash() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = HASHER.hash(DUMMY_PASSWORD + secrets.token_hex(8))
    return _dummy_hash


def hash_password(password: str) -> str:
    return HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return HASHER.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def bootstrap_admin(cfg: Config, conn) -> None:
    existing = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if not existing:
        if cfg.admin_password:
            password = cfg.admin_password
            must_change = 0
        else:
            password = secrets.token_urlsafe(20)
            must_change = 1
            cfg.bootstrap_path.write_text(password + "\n", encoding="utf-8")
            os.chmod(cfg.bootstrap_path, 0o600)
            emit("admin_bootstrap_written", path=str(cfg.bootstrap_path))
        conn.execute(
            """
            INSERT INTO users (username, password_hash, must_change_password, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (cfg.admin_user, hash_password(password), must_change, iso(_utcnow())),
        )
        return
    # Start script --password is ignored unless we sync: the first user already exists.
    if not cfg.admin_password:
        return
    user = get_user_by_username(conn, cfg.admin_user)
    if user is None:
        conn.execute(
            """
            INSERT INTO users (username, password_hash, must_change_password, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (cfg.admin_user, hash_password(cfg.admin_password), 0, iso(_utcnow())),
        )
        emit("admin_created", user=cfg.admin_user)
        return
    if verify_password(user.password_hash, cfg.admin_password):
        return
    conn.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
        (hash_password(cfg.admin_password), user.id),
    )
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
    conn.execute("DELETE FROM login_attempts")
    emit("admin_password_synced", user=user.username)


def create_session(conn, user: User, *, days: int, ip: str | None, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = _utcnow()
    expires = now + timedelta(days=days)
    conn.execute(
        """
        INSERT INTO sessions (id, user_id, created_at, expires_at, last_seen_at, ip, user_agent)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            hash_session_token(token),
            user.id,
            iso(now),
            iso(expires),
            iso(now),
            ip,
            (user_agent or "")[:256],
        ),
    )
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (iso(now), user.id))
    return token


def load_session_user(conn, token: str | None) -> User | None:
    if not token:
        return None
    sid = hash_session_token(token)
    row = conn.execute(
        """
        SELECT s.expires_at AS expires_at, u.*
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.id = ?
        """,
        (sid,),
    ).fetchone()
    if row is None:
        return None
    expires = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if expires < _utcnow():
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        return None
    conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (iso(_utcnow()), sid))
    return user_from_row(row)


def delete_session(conn, token: str | None) -> None:
    if not token:
        return
    conn.execute("DELETE FROM sessions WHERE id = ?", (hash_session_token(token),))


def delete_user_sessions(conn, user_id: int) -> None:
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def change_password(conn, user: User, new_password: str) -> None:
    conn.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
        (hash_password(new_password), user.id),
    )
    delete_user_sessions(conn, user.id)


def authenticate(conn, username: str, password: str) -> User | None:
    user = get_user_by_username(conn, username)
    if user is None:
        verify_password(dummy_hash(), password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    return user
