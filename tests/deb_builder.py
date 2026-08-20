"""Build minimal but real .deb files for tests (ar + tar + gzip/zstd)."""

from __future__ import annotations

import gzip
import io
import tarfile
import time
from pathlib import Path

import zstandard


def _ar_header(name: str, size: int) -> bytes:
    name_f = (name + "/").ljust(16)
    date_f = str(int(time.time())).ljust(12)
    uid_f = "0".ljust(6)
    gid_f = "0".ljust(6)
    mode_f = "100644".ljust(8)
    size_f = str(size).ljust(10)
    return f"{name_f}{date_f}{uid_f}{gid_f}{mode_f}{size_f}`\n".encode("ascii")


def _ar_member(name: str, data: bytes) -> bytes:
    body = _ar_header(name, len(data)) + data
    if len(data) % 2 == 1:
        body += b"\n"
    return body


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        dirs: set[str] = set()
        for name in files:
            parts = name.lstrip("./").split("/")
            acc = "."
            for part in parts[:-1]:
                acc = f"{acc}/{part}"
                dirs.add(acc)
        for directory in sorted(dirs, key=lambda item: item.count("/")):
            info = tarfile.TarInfo(name=directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            tf.addfile(info)
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = 0
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def build_deb(
    dest: Path,
    *,
    package: str = "hello-prv",
    version: str = "1.0-1",
    architecture: str = "all",
    maintainer: str = "Operator <ops@example.net>",
    description: str = "example arch-all package\nThis is the extended description.\n\nSecond paragraph.",
    extra_fields: dict[str, str] | None = None,
    control_compress: str = "gz",
    essential: bool = False,
    include_data: bool = True,
    debian_binary: bytes = b"2.0\n",
) -> Path:
    fields = [
        f"Package: {package}",
        f"Version: {version}",
        f"Architecture: {architecture}",
        f"Maintainer: {maintainer}",
        "Installed-Size: 12",
        "Section: utils",
        "Priority: optional",
        "Homepage: https://example.net/hello-prv",
        f"Description: {description.splitlines()[0]}",
    ]
    for line in description.splitlines()[1:]:
        if line == "":
            fields.append(" .")
        else:
            fields.append(" " + line)
    if essential:
        fields.append("Essential: yes")
    if extra_fields:
        for key, value in extra_fields.items():
            fields.append(f"{key}: {value}")
    control = ("\n".join(fields) + "\n").encode("utf-8")
    control_tar = _tar_bytes({"./control": control})
    if control_compress == "zst":
        control_member = "control.tar.zst"
        control_blob = zstandard.ZstdCompressor().compress(control_tar)
    elif control_compress == "gz":
        control_member = "control.tar.gz"
        control_blob = gzip.compress(control_tar, mtime=0)
    else:
        raise ValueError(control_compress)

    data_files = {}
    if include_data:
        data_files[f"./usr/share/doc/{package}/README"] = b"hello from fixture\n"
    data_tar = gzip.compress(_tar_bytes(data_files) if data_files else _tar_bytes({}), mtime=0)

    blob = b"!<arch>\n"
    blob += _ar_member("debian-binary", debian_binary)
    blob += _ar_member(control_member, control_blob)
    if include_data:
        blob += _ar_member("data.tar.gz", data_tar)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    return dest
