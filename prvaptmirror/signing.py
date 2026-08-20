"""GPG key bootstrap and InRelease / Release.gpg signing."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from prvaptmirror.config import Config
from prvaptmirror.db import get_setting, set_setting
from prvaptmirror.events import emit


class SigningError(RuntimeError):
    pass


def _gpg_env(cfg: Config) -> dict[str, str]:
    env = os.environ.copy()
    env["GNUPGHOME"] = str(cfg.gnupg_dir)
    env.pop("GPG_AGENT_INFO", None)
    return env


def _passphrase_args(cfg: Config) -> list[str]:
    args = ["--pinentry-mode", "loopback"]
    if cfg.gpg_passphrase_file is not None:
        args.extend(["--passphrase-file", str(cfg.gpg_passphrase_file)])
    else:
        args.extend(["--passphrase", ""])
    return args


def _run_gpg(cfg: Config, args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = ["gpg", "--homedir", str(cfg.gnupg_dir), "--batch", "--yes", *args]
    return subprocess.run(
        cmd,
        env=_gpg_env(cfg),
        check=False,
        capture_output=True,
        timeout=timeout,
    )


def list_secret_fingerprints(cfg: Config) -> list[str]:
    proc = _run_gpg(cfg, ["--list-secret-keys", "--with-colons"])
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        if "need_entropy" in err:
            raise SigningError(err)
        # empty keyring is ok
        return []
    fprs: list[str] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(":")
        if parts and parts[0] == "fpr" and len(parts) > 9:
            fprs.append(parts[9])
    # unique preserving order (subkeys also emit fpr; take 40-char primary-ish)
    seen: list[str] = []
    for fpr in fprs:
        if len(fpr) >= 40 and fpr not in seen:
            seen.append(fpr)
    return seen


def _export_public(cfg: Config, fingerprint: str) -> None:
    cfg.repo_dir.mkdir(parents=True, exist_ok=True)
    armored = _run_gpg(cfg, ["--armor", "--export", fingerprint])
    if armored.returncode != 0:
        raise SigningError(armored.stderr.decode("utf-8", "replace"))
    cfg.pubkey_path.write_bytes(armored.stdout)
    os.chmod(cfg.pubkey_path, 0o644)
    binary = _run_gpg(cfg, ["--export", fingerprint])
    if binary.returncode != 0:
        raise SigningError(binary.stderr.decode("utf-8", "replace"))
    cfg.keyring_path.write_bytes(binary.stdout)
    os.chmod(cfg.keyring_path, 0o644)


def ensure_key(cfg: Config, conn) -> str:
    cfg.gnupg_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.gnupg_dir, 0o700)
    keys = list_secret_fingerprints(cfg)
    stored = get_setting(conn, "gpg_fingerprint")
    configured = cfg.gpg_fingerprint
    if len(keys) == 0:
        started = time.monotonic()
        proc = _run_gpg(
            cfg,
            [
                *_passphrase_args(cfg),
                "--quick-generate-key",
                cfg.gpg_uid,
                "rsa4096",
                "sign",
                "never",
            ],
            timeout=120,
        )
        elapsed = time.monotonic() - started
        if elapsed > 30:
            emit("gpg_keygen_slow", seconds=round(elapsed, 1))
        if proc.returncode != 0:
            raise SigningError(proc.stderr.decode("utf-8", "replace") or "gpg keygen failed")
        keys = list_secret_fingerprints(cfg)
        if len(keys) != 1:
            raise SigningError("gpg keygen produced an unexpected keyring")
        fingerprint = keys[0]
    elif len(keys) == 1:
        fingerprint = keys[0]
        if configured and configured.replace(" ", "").upper() != fingerprint.upper():
            raise SigningError("PRVAPT_GPG_FINGERPRINT does not match the only secret key")
    else:
        if not configured:
            raise SigningError("multiple secret keys in GNUPGHOME; set PRVAPT_GPG_FINGERPRINT")
        want = configured.replace(" ", "").upper()
        match = [k for k in keys if k.upper() == want or k.upper().endswith(want)]
        if len(match) != 1:
            raise SigningError("PRVAPT_GPG_FINGERPRINT does not uniquely match a secret key")
        fingerprint = match[0]
    set_setting(conn, "gpg_fingerprint", fingerprint)
    _export_public(cfg, fingerprint)
    return fingerprint


def sign_release(cfg: Config, suite_dir: Path, fingerprint: str) -> None:
    release = suite_dir / "Release"
    inrelease = suite_dir / "InRelease"
    det = suite_dir / "Release.gpg"
    inrelease.unlink(missing_ok=True)
    det.unlink(missing_ok=True)
    common = [
        *_passphrase_args(cfg),
        "--digest-algo",
        "SHA256",
        "--default-key",
        fingerprint,
    ]
    proc = _run_gpg(
        cfg,
        [*common, "--clearsign", "--output", str(inrelease), str(release)],
    )
    if proc.returncode != 0 or not inrelease.is_file():
        raise SigningError(proc.stderr.decode("utf-8", "replace") or "clearsign failed")
    proc = _run_gpg(
        cfg,
        [*common, "--detach-sign", "--armor", "--output", str(det), str(release)],
    )
    if proc.returncode != 0 or not det.is_file():
        raise SigningError(proc.stderr.decode("utf-8", "replace") or "detach-sign failed")
    verify = subprocess.run(
        ["gpgv", "--keyring", str(cfg.keyring_path), str(inrelease)],
        capture_output=True,
        env=_gpg_env(cfg),
        check=False,
    )
    if verify.returncode != 0:
        raise SigningError(
            "gpgv failed: " + verify.stderr.decode("utf-8", "replace")
        )
    os.chmod(inrelease, 0o644)
    os.chmod(det, 0o644)
