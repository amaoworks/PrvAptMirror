"""Environment-driven configuration."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlparse


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() in {"localhost", "localhost."}:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def public_url_is_loopback(url: str) -> bool:
    return is_loopback_host(urlparse(url).hostname)


@dataclass(frozen=True)
class Config:
    data_dir: Path
    secret_key: str
    admin_user: str
    admin_password: str | None
    public_url: str
    admin_origins: frozenset[str]
    cookie_secure: str
    trusted_proxy_cidrs: tuple
    origin_check: bool
    insecure_no_auth: bool
    max_upload_mb: int
    max_upload_files: int
    suite: str
    codename: str
    component: str
    architectures: tuple[str, ...]
    origin: str
    label: str
    gpg_uid: str
    gpg_fingerprint: str | None
    gpg_passphrase_file: Path | None
    session_days: int

    @property
    def repo_dir(self) -> Path:
        return self.data_dir / "repo"

    @property
    def gnupg_dir(self) -> Path:
        return self.data_dir / "gnupg"

    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "incoming"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "data.sqlite"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "publish.lock"

    @property
    def bootstrap_path(self) -> Path:
        return self.data_dir / "admin-bootstrap.txt"

    @property
    def pubkey_path(self) -> Path:
        return self.repo_dir / "pubkey.asc"

    @property
    def keyring_path(self) -> Path:
        return self.repo_dir / "keyring.gpg"

    @property
    def dists_dir(self) -> Path:
        return self.repo_dir / "dists"

    @property
    def pool_dir(self) -> Path:
        return self.repo_dir / "pool"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def apt_base(self) -> str:
        return self.public_url.rstrip("/") + "/apt"


def _ensure_secret(data_dir: Path) -> str:
    env = os.environ.get("PRVAPT_SECRET_KEY", "").strip()
    if env:
        return env
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "secret-key"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(48)
    path.write_text(secret + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return secret


def load_config(environ: dict[str, str] | None = None) -> Config:
    env = os.environ if environ is None else environ
    data_dir = Path(env.get("PRVAPT_DATA_DIR", "/var/lib/prvaptmirror")).resolve()
    public_url = env.get("PRVAPT_PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/")
    origins = env.get(
        "PRVAPT_ADMIN_ORIGINS",
        "http://127.0.0.1:8080,http://localhost:8080",
    )
    cidrs_raw = env.get(
        "PRVAPT_TRUSTED_PROXY_CIDRS",
        "172.16.0.0/12,127.0.0.1/32,10.0.0.0/8",
    )
    cidrs = tuple(ip_network(item, strict=False) for item in _csv(cidrs_raw))
    pw = env.get("PRVAPT_ADMIN_PASSWORD", "").strip() or None
    fpr = env.get("PRVAPT_GPG_FINGERPRINT", "").strip() or None
    pass_file = env.get("PRVAPT_GPG_PASSPHRASE_FILE", "").strip()
    insecure = env.get("PRVAPT_INSECURE_NO_AUTH", "").strip() in {"1", "true", "TRUE", "yes"}
    origin_check = env.get("PRVAPT_ORIGIN_CHECK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    archs = tuple(_csv(env.get("PRVAPT_ARCHS", "amd64,arm64,all")))
    if not archs:
        archs = ("amd64", "arm64", "all")
    return Config(
        data_dir=data_dir,
        secret_key=_ensure_secret(data_dir),
        admin_user=env.get("PRVAPT_ADMIN_USER", "admin").strip() or "admin",
        admin_password=pw,
        public_url=public_url,
        admin_origins=frozenset(item.rstrip("/") for item in _csv(origins)),
        cookie_secure=env.get("PRVAPT_COOKIE_SECURE", "auto").strip().lower() or "auto",
        trusted_proxy_cidrs=cidrs,
        origin_check=origin_check,
        insecure_no_auth=insecure,
        max_upload_mb=int(env.get("PRVAPT_MAX_UPLOAD_MB", "512")),
        max_upload_files=int(env.get("PRVAPT_MAX_UPLOAD_FILES", "20")),
        suite=env.get("PRVAPT_SUITE", "stable").strip() or "stable",
        codename=env.get("PRVAPT_CODENAME", "stable").strip() or "stable",
        component=env.get("PRVAPT_COMPONENT", "main").strip() or "main",
        architectures=archs,
        origin=env.get("PRVAPT_ORIGIN", "PrvAptMirror").strip() or "PrvAptMirror",
        label=env.get("PRVAPT_LABEL", "prvapt").strip() or "prvapt",
        gpg_uid=env.get("PRVAPT_GPG_UID", "PrvAptMirror <apt@localhost>"),
        gpg_fingerprint=fpr,
        gpg_passphrase_file=Path(pass_file) if pass_file else None,
        session_days=int(env.get("PRVAPT_SESSION_DAYS", "7")),
    )


def validate_startup(cfg: Config) -> None:
    if cfg.insecure_no_auth and not public_url_is_loopback(cfg.public_url):
        raise RuntimeError(
            "PRVAPT_INSECURE_NO_AUTH=1 is only allowed when PRVAPT_PUBLIC_URL "
            "points at a loopback host"
        )
    if cfg.cookie_secure not in {"auto", "true", "false"}:
        raise RuntimeError("PRVAPT_COOKIE_SECURE must be auto, true, or false")


def ensure_data_dirs(cfg: Config) -> None:
    os.umask(0o022)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.gnupg_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.gnupg_dir, 0o700)
    cfg.incoming_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.incoming_dir, 0o700)
    cfg.staging_dir.mkdir(parents=True, exist_ok=True)
    cfg.repo_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.repo_dir, 0o755)
    (cfg.repo_dir / "pool").mkdir(exist_ok=True)
    os.chmod(cfg.repo_dir / "pool", 0o755)
