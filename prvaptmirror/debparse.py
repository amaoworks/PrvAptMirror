"""Parse and validate .deb control metadata. Does not unpack data.tar or run scripts."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import lzma
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import zstandard
from debian.debian_support import Version

PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
ALLOWED_CONTENT_TYPES = {
    "application/vnd.debian.binary-package",
    "application/x-debian-package",
    "application/octet-stream",
}

CONTROL_FIELDS = (
    "Package",
    "Version",
    "Architecture",
    "Maintainer",
    "Installed-Size",
    "Depends",
    "Pre-Depends",
    "Recommends",
    "Suggests",
    "Conflicts",
    "Breaks",
    "Replaces",
    "Provides",
    "Enhances",
    "Section",
    "Priority",
    "Homepage",
    "Description",
    "Multi-Arch",
    "Built-Using",
    "Source",
)


class DebParseError(ValueError):
    """Structured validation failure; str() is operator-facing Chinese/English mix."""


@dataclass
class ParsedDeb:
    name: str
    version: str
    architecture: str
    control: dict[str, str]
    size: int
    md5: str
    sha1: str
    sha256: str
    warnings: list[str] = field(default_factory=list)

    def control_json(self) -> str:
        return json.dumps(self.control, ensure_ascii=False, sort_keys=False)


def hash_file(path: Path) -> tuple[int, str, str, str]:
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
    return size, md5.hexdigest(), sha1.hexdigest(), sha256.hexdigest()


def _read_ar_members(path: Path) -> list[tuple[str, bytes | None]]:
    """Return (name, data) for debian-binary and control.tar*; data.tar is skipped."""
    members: list[tuple[str, bytes | None]] = []
    with path.open("rb") as fh:
        magic = fh.read(8)
        if magic != b"!<arch>\n":
            raise DebParseError("不是 Unix ar 归档（.deb 必须以 !<arch> 开头）")
        while True:
            hdr = fh.read(60)
            if not hdr:
                break
            if len(hdr) < 60:
                raise DebParseError("ar 头截断")
            raw_name = hdr[0:16].decode("ascii", "replace").strip()
            name = raw_name.rstrip("/")
            try:
                size = int(hdr[48:58].strip() or b"0")
            except ValueError as exc:
                raise DebParseError("ar 成员 size 非法") from exc
            if name.startswith("debian-binary") or name.startswith("control.tar"):
                data = fh.read(size)
                if len(data) != size:
                    raise DebParseError(f"ar 成员 {name} 截断")
                members.append((name, data))
            else:
                fh.seek(size, 1)
                members.append((name, None))
            if size % 2 == 1:
                fh.read(1)
    return members


def _decompress_tar(name: str, blob: bytes) -> bytes:
    lower = name.lower()
    if lower.endswith(".tar.zst") or lower.endswith(".tar.zstd"):
        try:
            return zstandard.ZstdDecompressor().decompress(blob)
        except zstandard.ZstdError as exc:
            raise DebParseError("无法解压 control.tar.zst（损坏或内部错误）") from exc
    if lower.endswith(".tar.gz") or lower.endswith(".tar.gzip"):
        return gzip.decompress(blob)
    if lower.endswith(".tar.xz"):
        return lzma.decompress(blob)
    if lower.endswith(".tar.bz2"):
        import bz2

        return bz2.decompress(blob)
    if lower.endswith(".tar"):
        return blob
    raise DebParseError(f"不支持的 control 压缩: {name}")


def _control_from_tar(tar_bytes: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
        for member in tf.getmembers():
            base = member.name.split("/")[-1]
            if base == "control" and member.isfile():
                extracted = tf.extractfile(member)
                if extracted is None:
                    break
                return extracted.read()
    raise DebParseError("control.tar 中没有 control 文件")


def _parse_control_text(raw: bytes) -> dict[str, str]:
    text = raw.decode("utf-8")
    fields: dict[str, str] = []
    # ordered dict via insertion
    result: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal current, buf
        if current is None:
            return
        value = "\n".join(buf).rstrip("\n")
        result[current] = value
        current = None
        buf = []

    for line in text.splitlines():
        if not line:
            continue
        if line.startswith(" ") or line.startswith("\t"):
            if current is None:
                raise DebParseError("control 续行没有对应字段")
            rest = line[1:]
            if rest == ".":
                buf.append("")
            else:
                buf.append(rest)
            continue
        flush()
        if ":" not in line:
            raise DebParseError(f"control 字段缺少冒号: {line[:80]}")
        key, _, val = line.partition(":")
        current = key.strip()
        buf = [val.lstrip()]
    flush()
    return result


def _unsafe_path_fragment(value: str, *, allow_colon: bool) -> bool:
    if "\x00" in value or "/" in value or "\\" in value or ".." in value:
        return True
    if not allow_colon and ":" in value:
        return True
    return False


def parse_deb(path: Path, allowed_archs: tuple[str, ...] | None = None) -> ParsedDeb:
    if path.suffix.lower() != ".deb":
        raise DebParseError("扩展名必须是 .deb")
    members = _read_ar_members(path)
    names = [n for n, _ in members]
    if not any(n.startswith("debian-binary") for n in names):
        raise DebParseError("缺少 debian-binary 成员")
    if not any(n.startswith("control.tar") for n in names):
        raise DebParseError("缺少 control.tar* 成员")
    if not any(n.startswith("data.tar") for n in names):
        raise DebParseError("缺少 data.tar* 成员")

    debian_binary = next(data for n, data in members if n.startswith("debian-binary") and data is not None)
    if not debian_binary.startswith(b"2."):
        raise DebParseError("仅支持 debian-binary 2.x 格式")

    control_name, control_blob = next(
        (n, data) for n, data in members if n.startswith("control.tar") and data is not None
    )
    try:
        tar_bytes = _decompress_tar(control_name, control_blob)
    except DebParseError:
        raise
    except Exception as exc:
        if "zst" in control_name.lower():
            raise DebParseError("无法解压 control.tar.zst（内部错误，应安装 zstandard）") from exc
        raise DebParseError(f"无法解压 {control_name}") from exc

    control_raw = _control_from_tar(tar_bytes)
    raw_fields = _parse_control_text(control_raw)

    warnings: list[str] = []
    if "Essential" in raw_fields:
        warnings.append("discarded Essential field")
        raw_fields.pop("Essential", None)

    for required in ("Package", "Version", "Architecture"):
        if required not in raw_fields or not raw_fields[required].strip():
            raise DebParseError(f"control 缺少必填字段 {required}")

    name = raw_fields["Package"].strip()
    version = raw_fields["Version"].strip()
    architecture = raw_fields["Architecture"].strip()

    if not PACKAGE_RE.match(name):
        raise DebParseError(f"非法 Package 名: {name}")
    if _unsafe_path_fragment(name, allow_colon=False):
        raise DebParseError("Package 名含非法路径字符")
    if _unsafe_path_fragment(architecture, allow_colon=False):
        raise DebParseError("Architecture 含非法路径字符")
    if _unsafe_path_fragment(version, allow_colon=True):
        raise DebParseError("Version 含非法路径字符")

    try:
        Version(version)
    except Exception as exc:
        raise DebParseError(f"无法解析 Version: {version}") from exc

    if allowed_archs is not None and architecture not in allowed_archs:
        raise DebParseError(
            f"Architecture {architecture} 不在当前允许列表 {','.join(allowed_archs)}；"
            "把该 arch 加入 PRVAPT_ARCHS 后重建索引"
        )

    control: dict[str, str] = {}
    for key in CONTROL_FIELDS:
        if key in raw_fields and raw_fields[key] != "":
            control[key] = raw_fields[key]

    size, md5, sha1, sha256 = hash_file(path)
    return ParsedDeb(
        name=name,
        version=version,
        architecture=architecture,
        control=control,
        size=size,
        md5=md5,
        sha1=sha1,
        sha256=sha256,
        warnings=warnings,
    )
