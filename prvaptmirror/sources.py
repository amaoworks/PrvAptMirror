"""Persistent package-source configuration and encrypted credentials."""

from __future__ import annotations

import base64
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from prvaptmirror.config import Config
from prvaptmirror.models import SourceRow

SOURCE_KINDS = {"github_release", "direct_url"}
DEFAULT_ASSET_PATTERN = r".*\.deb$"


class SourceValidationError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fernet(cfg: Config) -> Fernet:
    digest = hashlib.sha256(
        b"prvaptmirror/source-token/v1\0" + cfg.secret_key.encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(cfg: Config, token: str) -> str:
    value = token.strip()
    if not value:
        return ""
    return _fernet(cfg).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_token(cfg: Config, encrypted: str | None) -> str | None:
    if not encrypted:
        return None
    try:
        return _fernet(cfg).decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise RuntimeError("软件来源凭据无法解密；应用密钥可能已经改变") from exc


def normalize_github_repository(raw: str) -> str:
    value = raw.strip()
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc.lower() not in {
            "github.com",
            "www.github.com",
        }:
            raise SourceValidationError("GitHub 来源必须是 github.com 地址或 owner/repository")
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) < 2:
            raise SourceValidationError("GitHub 仓库地址缺少 owner/repository")
        value = "/".join(parts[:2])
    if value.endswith(".git"):
        value = value[:-4]
    parts = value.split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts):
        raise SourceValidationError("GitHub 仓库格式必须是 owner/repository")
    return value


def normalize_direct_url(raw: str) -> str:
    value = raw.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SourceValidationError("下载地址必须是完整的 http:// 或 https:// URL")
    if parsed.username or parsed.password:
        raise SourceValidationError("下载地址不能包含用户名或密码")
    if len(value) > 2048:
        raise SourceValidationError("下载地址过长")
    return value


def validate_github_probe_values(
    location: str, asset_pattern: str, release_limit: str
) -> tuple[str, str, int]:
    """Validate the GitHub fields shared by source saves and live previews."""
    repository = normalize_github_repository(location)
    pattern = asset_pattern.strip() or DEFAULT_ASSET_PATTERN
    if len(pattern) > 500:
        raise SourceValidationError("Asset 匹配表达式不能超过 500 个字符")
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise SourceValidationError(f"Asset 匹配表达式无效：{exc}") from exc
    try:
        limit = int(release_limit)
    except ValueError as exc:
        raise SourceValidationError("Release 数量必须是整数") from exc
    if not 1 <= limit <= 20:
        raise SourceValidationError("Release 数量必须在 1 到 20 之间")
    return repository, pattern, limit


def validate_source_values(values: Mapping[str, str]) -> dict[str, object]:
    name = values.get("name", "").strip()
    if not name or len(name) > 80:
        raise SourceValidationError("来源名称不能为空且不能超过 80 个字符")
    kind = values.get("kind", "").strip()
    if kind not in SOURCE_KINDS:
        raise SourceValidationError("不支持的软件来源类型")
    location = values.get("location", "").strip()
    if kind == "github_release":
        location, asset_pattern, release_limit = validate_github_probe_values(
            location,
            values.get("asset_pattern", ""),
            values.get("release_limit", "1"),
        )
    else:
        location = normalize_direct_url(location)
        asset_pattern = values.get("asset_pattern", "").strip() or DEFAULT_ASSET_PATTERN
        if len(asset_pattern) > 500:
            raise SourceValidationError("Asset 匹配表达式不能超过 500 个字符")
        try:
            re.compile(asset_pattern, re.IGNORECASE)
        except re.error as exc:
            raise SourceValidationError(f"Asset 匹配表达式无效：{exc}") from exc
        try:
            release_limit = int(values.get("release_limit", "1"))
        except ValueError as exc:
            raise SourceValidationError("Release 数量必须是整数") from exc
        if not 1 <= release_limit <= 20:
            raise SourceValidationError("Release 数量必须在 1 到 20 之间")
    try:
        interval_minutes = int(values.get("interval_minutes", "30"))
    except ValueError as exc:
        raise SourceValidationError("检查间隔必须是整数") from exc
    if not 5 <= interval_minutes <= 10080:
        raise SourceValidationError("检查间隔必须在 5 到 10080 分钟之间")
    return {
        "name": name,
        "kind": kind,
        "location": location,
        "asset_pattern": asset_pattern,
        "interval_minutes": interval_minutes,
        "release_limit": release_limit,
        "include_prereleases": values.get("include_prereleases", "") == "yes",
        "enabled": values.get("enabled", "") == "yes",
    }


def source_from_row(row) -> SourceRow:
    return SourceRow.from_row(row)


def list_sources(conn: sqlite3.Connection) -> list[SourceRow]:
    return [
        source_from_row(row)
        for row in conn.execute("SELECT * FROM package_sources ORDER BY name COLLATE NOCASE")
    ]


def get_source(conn: sqlite3.Connection, source_id: int) -> SourceRow | None:
    row = conn.execute("SELECT * FROM package_sources WHERE id = ?", (source_id,)).fetchone()
    return source_from_row(row) if row else None


def create_source(
    cfg: Config,
    conn: sqlite3.Connection,
    values: Mapping[str, str],
    token: str = "",
) -> SourceRow:
    clean = validate_source_values(values)
    now = now_iso()
    try:
        cur = conn.execute(
            """
            INSERT INTO package_sources (
              name, kind, location, asset_pattern, interval_minutes, release_limit,
              include_prereleases, enabled, token_encrypted, created_at, updated_at,
              next_check_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean["name"],
                clean["kind"],
                clean["location"],
                clean["asset_pattern"],
                clean["interval_minutes"],
                clean["release_limit"],
                int(bool(clean["include_prereleases"])),
                int(bool(clean["enabled"])),
                encrypt_token(cfg, token) or None,
                now,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise SourceValidationError("来源名称已经存在") from exc
    source = get_source(conn, int(cur.lastrowid))
    assert source is not None
    return source


def update_source(
    cfg: Config,
    conn: sqlite3.Connection,
    source_id: int,
    values: Mapping[str, str],
    *,
    token: str = "",
    clear_token: bool = False,
) -> SourceRow:
    current = get_source(conn, source_id)
    if current is None:
        raise SourceValidationError("软件来源不存在")
    clean = validate_source_values(values)
    encrypted = current.token_encrypted
    if clear_token:
        encrypted = None
    elif token.strip():
        encrypted = encrypt_token(cfg, token)
    try:
        conn.execute(
            """
            UPDATE package_sources SET
              name = ?, kind = ?, location = ?, asset_pattern = ?,
              interval_minutes = ?, release_limit = ?, include_prereleases = ?,
              enabled = ?, token_encrypted = ?, updated_at = ?, next_check_at = ?
            WHERE id = ?
            """,
            (
                clean["name"],
                clean["kind"],
                clean["location"],
                clean["asset_pattern"],
                clean["interval_minutes"],
                clean["release_limit"],
                int(bool(clean["include_prereleases"])),
                int(bool(clean["enabled"])),
                encrypted,
                now_iso(),
                now_iso(),
                source_id,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise SourceValidationError("来源名称已经存在") from exc
    source = get_source(conn, source_id)
    assert source is not None
    return source


def delete_source(conn: sqlite3.Connection, source_id: int) -> bool:
    cur = conn.execute("DELETE FROM package_sources WHERE id = ?", (source_id,))
    return cur.rowcount > 0


def queue_source(conn: sqlite3.Connection, source_id: int) -> bool:
    cur = conn.execute(
        """
        UPDATE package_sources
        SET next_check_at = ?, last_status = 'queued'
        WHERE id = ? AND enabled = 1 AND last_status != 'running'
        """,
        (now_iso(), source_id),
    )
    return cur.rowcount > 0


def source_runs(conn: sqlite3.Connection, source_id: int, limit: int = 20):
    return conn.execute(
        "SELECT * FROM source_runs WHERE source_id = ? ORDER BY id DESC LIMIT ?",
        (source_id, limit),
    ).fetchall()


def source_artifacts(conn: sqlite3.Connection, source_id: int, limit: int = 50):
    return conn.execute(
        "SELECT * FROM source_artifacts WHERE source_id = ? ORDER BY id DESC LIMIT ?",
        (source_id, limit),
    ).fetchall()
