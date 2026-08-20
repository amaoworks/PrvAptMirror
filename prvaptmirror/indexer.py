"""Render Packages / Release according to DebianRepository/Format."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from debian.debian_support import Version

from prvaptmirror.config import Config
from prvaptmirror.debparse import CONTROL_FIELDS
from prvaptmirror.models import PackageRow

PACKAGE_FIELD_ORDER = CONTROL_FIELDS + (
    "Filename",
    "Size",
    "MD5sum",
    "SHA1",
    "SHA256",
)

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_rfc5322_utc(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return (
        f"{DAYS[dt.weekday()]}, {dt.day:02d} {MONTHS[dt.month - 1]} {dt.year} "
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} UTC"
    )


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    os.chmod(path, 0o644)


def write_gzip_stable(path: Path, body: bytes) -> None:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(body)
    write_atomic(path, buf.getvalue())


def _format_field(name: str, value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    parts = value.split("\n")
    if len(parts) == 1:
        return f"{name}: {parts[0]}"
    lines = [f"{name}: {parts[0]}"]
    for part in parts[1:]:
        if part == "":
            lines.append(" .")
        elif part.startswith(" "):
            lines.append(part)
        else:
            lines.append(" " + part)
    return "\n".join(lines)


def render_packages(rows: list[PackageRow]) -> bytes:
    if not rows:
        return b""

    def sort_key(row: PackageRow) -> tuple:
        return (row.name, Version(row.version), row.architecture)

    stanzas: list[str] = []
    for row in sorted(rows, key=sort_key):
        control = json.loads(row.control_json)
        lines: list[str] = []
        for field in PACKAGE_FIELD_ORDER:
            if field == "Filename":
                lines.append(f"Filename: {row.filename}")
            elif field == "Size":
                lines.append(f"Size: {row.size}")
            elif field == "MD5sum":
                lines.append(f"MD5sum: {row.md5}")
            elif field == "SHA1":
                lines.append(f"SHA1: {row.sha1}")
            elif field == "SHA256":
                lines.append(f"SHA256: {row.sha256}")
            else:
                value = control.get(field)
                if not value:
                    continue
                lines.append(_format_field(field, str(value)))
        stanzas.append("\n".join(lines))
    return ("\n\n".join(stanzas) + "\n").encode("utf-8")


def render_arch_release(cfg: Config, arch: str) -> bytes:
    body = (
        f"Archive: {cfg.suite}\n"
        f"Origin: {cfg.origin}\n"
        f"Label: {cfg.label}\n"
        f"Acquire-By-Hash: no\n"
        f"Component: {cfg.component}\n"
        f"Architecture: {arch}\n"
    )
    return body.encode("utf-8")


def _hash_bytes(data: bytes) -> tuple[str, str, str]:
    return (
        hashlib.md5(data).hexdigest(),
        hashlib.sha1(data).hexdigest(),
        hashlib.sha256(data).hexdigest(),
    )


def render_release(cfg: Config, staging_suite: Path, *, now: datetime | None = None) -> bytes:
    now = now or datetime.now(timezone.utc)
    files: list[tuple[str, bytes]] = []
    component = cfg.component
    for arch in cfg.architectures:
        d = staging_suite / component / f"binary-{arch}"
        for name in ("Packages", "Packages.gz", "Release"):
            path = d / name
            data = path.read_bytes()
            rel = f"{component}/binary-{arch}/{name}"
            files.append((rel, data))
    files.sort(key=lambda item: item[0])
    md5_lines = []
    sha1_lines = []
    sha256_lines = []
    for rel, data in files:
        md5, sha1, sha256 = _hash_bytes(data)
        size = len(data)
        md5_lines.append(f" {md5} {size:>16} {rel}")
        sha1_lines.append(f" {sha1} {size:>16} {rel}")
        sha256_lines.append(f" {sha256} {size:>16} {rel}")
    archs = " ".join(cfg.architectures)
    header = (
        f"Origin: {cfg.origin}\n"
        f"Label: {cfg.label}\n"
        f"Suite: {cfg.suite}\n"
        f"Codename: {cfg.codename}\n"
        f"Date: {format_rfc5322_utc(now)}\n"
        f"Architectures: {archs}\n"
        f"Components: {cfg.component}\n"
        f"Description: Personal apt repository\n"
        f"Acquire-By-Hash: no\n"
        f"MD5Sum:\n"
        + "\n".join(md5_lines)
        + "\nSHA1:\n"
        + "\n".join(sha1_lines)
        + "\nSHA256:\n"
        + "\n".join(sha256_lines)
        + "\n"
    )
    return header.encode("utf-8")


def rebuild_dists(
    packages: list[PackageRow], staging_suite: Path, cfg: Config, *, now: datetime | None = None
) -> list[PackageRow]:
    skipped: list[PackageRow] = []
    by_arch: dict[str, list[PackageRow]] = {a: [] for a in cfg.architectures}
    for pkg in packages:
        if pkg.state != "active":
            continue
        if pkg.architecture == "all":
            if "all" in by_arch:
                by_arch["all"].append(pkg)
            for arch in cfg.architectures:
                if arch != "all":
                    by_arch[arch].append(pkg)
        elif pkg.architecture in by_arch:
            by_arch[pkg.architecture].append(pkg)
        else:
            skipped.append(pkg)

    for arch, rows in by_arch.items():
        dest = staging_suite / cfg.component / f"binary-{arch}"
        dest.mkdir(parents=True, exist_ok=True)
        os.chmod(dest, 0o755)
        body = render_packages(rows)
        write_atomic(dest / "Packages", body)
        write_gzip_stable(dest / "Packages.gz", body)
        write_atomic(dest / "Release", render_arch_release(cfg, arch))

    write_atomic(staging_suite / "Release", render_release(cfg, staging_suite, now=now))
    return skipped
