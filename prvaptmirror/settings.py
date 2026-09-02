"""Typed, database-backed application settings.

Deployment settings (paths, sockets, proxy trust and secrets) stay in the
environment. Repository behavior is seeded from the environment once and is
then managed from the admin UI.
"""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urlparse

from prvaptmirror.config import Config, validate_startup
from prvaptmirror.db import set_setting

SETTING_PREFIX = "app."
SETTINGS_PENDING_KEY = "app.settings_pending"
APP_SETTING_NAMES = (
    "public_url",
    "suite",
    "codename",
    "component",
    "architectures",
    "origin",
    "label",
    "max_upload_mb",
    "max_upload_files",
    "session_days",
)
REPOSITORY_SETTING_NAMES = frozenset(
    {"suite", "codename", "component", "architectures", "origin", "label"}
)

_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_ARCH = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_PUBLIC_URL = re.compile(r"^[A-Za-z0-9:/._~%+\[\]-]+$")


class SettingsValidationError(ValueError):
    pass


def app_setting_values(cfg: Config) -> dict[str, str]:
    return {
        "public_url": cfg.public_url,
        "suite": cfg.suite,
        "codename": cfg.codename,
        "component": cfg.component,
        "architectures": ",".join(cfg.architectures),
        "origin": cfg.origin,
        "label": cfg.label,
        "max_upload_mb": str(cfg.max_upload_mb),
        "max_upload_files": str(cfg.max_upload_files),
        "session_days": str(cfg.session_days),
    }


def _bounded_int(name: str, raw: str, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsValidationError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise SettingsValidationError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _repo_name(name: str, raw: str) -> str:
    value = raw.strip()
    if not _REPO_NAME.fullmatch(value):
        raise SettingsValidationError(
            f"{name} 只能包含字母、数字、点、加号、减号和下划线，最长 64 个字符"
        )
    return value


def _display_text(name: str, raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > 100 or any(ord(char) < 32 for char in value):
        raise SettingsValidationError(f"{name} 必须是 1 到 100 个字符的单行文本")
    return value


def config_from_app_values(base: Config, values: dict[str, str]) -> Config:
    public_url = values.get("public_url", "").strip().rstrip("/")
    parsed = urlparse(public_url)
    try:
        parsed.port
    except ValueError as exc:
        raise SettingsValidationError("公开 URL 的端口无效") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) <= 32 or ord(char) == 127 for char in public_url)
        or not _PUBLIC_URL.fullmatch(public_url)
    ):
        raise SettingsValidationError("公开 URL 必须是无账号、查询参数和片段的 http(s) 地址")
    if len(public_url) > 2048:
        raise SettingsValidationError("公开 URL 过长")

    archs: list[str] = []
    for item in values.get("architectures", "").split(","):
        arch = item.strip().lower()
        if not arch:
            continue
        if not _ARCH.fullmatch(arch):
            raise SettingsValidationError(f"无效架构名称：{arch}")
        if arch not in archs:
            archs.append(arch)
    if not archs:
        raise SettingsValidationError("至少需要配置一个架构")
    if len(archs) > 16:
        raise SettingsValidationError("最多配置 16 个架构")

    cfg = replace(
        base,
        public_url=public_url,
        suite=_repo_name("Suite", values.get("suite", "")),
        codename=_repo_name("Codename", values.get("codename", "")),
        component=_repo_name("Component", values.get("component", "")),
        architectures=tuple(archs),
        origin=_display_text("Origin", values.get("origin", "")),
        label=_display_text("Label", values.get("label", "")),
        max_upload_mb=_bounded_int(
            "单次上传上限", values.get("max_upload_mb", ""), 1, 10240
        ),
        max_upload_files=_bounded_int(
            "单次文件数", values.get("max_upload_files", ""), 1, 100
        ),
        session_days=_bounded_int(
            "会话有效期", values.get("session_days", ""), 1, 365
        ),
    )
    validate_startup(cfg)
    return cfg


def ensure_app_settings(conn, seed: Config) -> None:
    """Seed environment/default values once; never overwrite UI changes."""
    for name, value in app_setting_values(seed).items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (SETTING_PREFIX + name, value),
        )
    conn.execute(
        "INSERT OR IGNORE INTO settings(key, value) VALUES (?, '0')",
        (SETTINGS_PENDING_KEY,),
    )


def load_app_config(base: Config, conn) -> Config:
    values = app_setting_values(base)
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE ?",
        (SETTING_PREFIX + "%",),
    ).fetchall()
    for row in rows:
        name = str(row["key"])[len(SETTING_PREFIX) :]
        if name in APP_SETTING_NAMES:
            values[name] = str(row["value"])
    return config_from_app_values(base, values)


def save_app_config(conn, cfg: Config) -> None:
    for name, value in app_setting_values(cfg).items():
        set_setting(conn, SETTING_PREFIX + name, value)


def repository_settings_changed(before: Config, after: Config) -> bool:
    old = app_setting_values(before)
    new = app_setting_values(after)
    return any(old[name] != new[name] for name in REPOSITORY_SETTING_NAMES)
