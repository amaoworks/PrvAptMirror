"""DB-backed login rate limit: 5 failures / 15 minutes per IP, then lock 15 minutes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from ipaddress import ip_address

from starlette.requests import Request

from prvaptmirror.config import Config

WINDOW_S = 15 * 60
MAX_FAILURES = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def client_ip(request: Request, cfg: Config) -> str:
    peer = request.client.host if request.client else "0.0.0.0"
    try:
        addr = ip_address(peer)
    except ValueError:
        return peer
    trusted = any(addr in net for net in cfg.trusted_proxy_cidrs)
    if trusted:
        forwarded = (request.headers.get("x-real-ip") or "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer


def cookie_secure_flag(request: Request, cfg: Config) -> bool:
    if cfg.cookie_secure == "true":
        return True
    if cfg.cookie_secure == "false":
        return False
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        peer = request.client.host if request.client else ""
        try:
            addr = ip_address(peer)
        except ValueError:
            addr = None
        if addr is not None and any(addr in net for net in cfg.trusted_proxy_cidrs):
            return proto == "https"
    return request.url.scheme == "https"


def is_locked(conn, ip: str) -> bool:
    cutoff = iso(_utcnow() - timedelta(seconds=WINDOW_S))
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM login_attempts
        WHERE ip = ? AND success = 0 AND at >= ?
        """,
        (ip, cutoff),
    ).fetchone()
    return int(row["n"]) >= MAX_FAILURES


def record_attempt(conn, ip: str, username: str | None, success: bool) -> None:
    conn.execute(
        "INSERT INTO login_attempts (ip, username, success, at) VALUES (?, ?, ?, ?)",
        (ip, username, 1 if success else 0, iso(_utcnow())),
    )
