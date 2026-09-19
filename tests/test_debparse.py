from pathlib import Path

import pytest

from prvaptmirror.debparse import DebParseError, parse_deb
from tests.deb_builder import build_deb


def test_parse_arch_all(tmp_path: Path):
    deb = build_deb(tmp_path / "hello-prv_1.0-1_all.deb", architecture="all")
    parsed = parse_deb(deb, allowed_archs=("amd64", "arm64", "all"))
    assert parsed.name == "hello-prv"
    assert parsed.version == "1.0-1"
    assert parsed.architecture == "all"
    assert parsed.control["Package"] == "hello-prv"
    assert "Essential" not in parsed.control
    assert parsed.size == deb.stat().st_size
    assert len(parsed.sha256) == 64


def test_parse_amd64_and_epoch(tmp_path: Path):
    deb = build_deb(
        tmp_path / "foo_1:2.0-1_amd64.deb",
        package="foo",
        version="1:2.0-1",
        architecture="amd64",
    )
    parsed = parse_deb(deb, allowed_archs=("amd64", "arm64", "all"))
    assert parsed.version == "1:2.0-1"
    assert parsed.architecture == "amd64"


def test_parse_control_tar_zst(tmp_path: Path):
    deb = build_deb(
        tmp_path / "zst_1.0-1_all.deb",
        package="zstpkg",
        control_compress="zst",
        zstd_write_content_size=False,
    )
    parsed = parse_deb(deb, allowed_archs=("amd64", "arm64", "all"))
    assert parsed.name == "zstpkg"


def test_normalizes_ascii_uppercase_package_name_without_rewriting_deb(tmp_path: Path):
    deb = build_deb(tmp_path / "Bettbox.deb", package="Bettbox")
    original = deb.read_bytes()

    parsed = parse_deb(deb, allowed_archs=("amd64", "arm64", "all"))

    assert parsed.name == "bettbox"
    assert parsed.control["Package"] == "bettbox"
    assert parsed.warnings == ["normalized Package field from 'Bettbox' to 'bettbox'"]
    assert deb.read_bytes() == original


def test_discards_essential(tmp_path: Path):
    deb = build_deb(tmp_path / "e_1.0-1_all.deb", essential=True)
    parsed = parse_deb(deb, allowed_archs=("amd64", "arm64", "all"))
    assert "Essential" not in parsed.control
    assert any("Essential" in w for w in parsed.warnings)


def test_rejects_unknown_arch(tmp_path: Path):
    deb = build_deb(tmp_path / "x_1.0-1_i386.deb", architecture="i386")
    with pytest.raises(DebParseError):
        parse_deb(deb, allowed_archs=("amd64", "arm64", "all"))


def test_rejects_non_ar(tmp_path: Path):
    path = tmp_path / "n.deb"
    path.write_bytes(b"not a deb")
    with pytest.raises(DebParseError):
        parse_deb(path)


def test_rejects_missing_control(tmp_path: Path):
    deb = build_deb(tmp_path / "bad.deb", include_data=True)
    # truncate to drop members
    data = deb.read_bytes()[:16]
    deb.write_bytes(data + b"xxxx")
    with pytest.raises(DebParseError):
        parse_deb(deb)


@pytest.mark.parametrize("compression", ["gz", "xz", "bz2", "zst", "tar"])
def test_control_decompression_has_a_hard_output_limit(monkeypatch, compression):
    import bz2
    import gzip
    import lzma
    import zstandard
    from prvaptmirror import debparse

    raw = b"x" * (128 * 1024)
    encode = {
        "gz": gzip.compress, "xz": lzma.compress, "bz2": bz2.compress,
        "zst": zstandard.ZstdCompressor(write_content_size=False).compress, "tar": lambda data: data,
    }[compression]
    monkeypatch.setattr(debparse, "MAX_CONTROL_ARCHIVE_BYTES", 64 * 1024)
    name = "control.tar" + ("" if compression == "tar" else "." + compression)
    with pytest.raises(DebParseError, match="超过大小限制"):
        debparse._decompress_tar(name, encode(raw))


def test_rejects_oversized_compressed_member_before_reading(tmp_path, monkeypatch):
    from prvaptmirror import debparse
    deb = build_deb(tmp_path / "oversized.deb")
    monkeypatch.setattr(debparse, "MAX_CONTROL_ARCHIVE_BYTES", 32)
    with pytest.raises(DebParseError, match="ar 成员 control.*超过大小限制"):
        parse_deb(deb)


def test_rejects_oversized_control_text(tmp_path, monkeypatch):
    from prvaptmirror import debparse
    deb = build_deb(tmp_path / "long-control.deb", description="x" * 4096)
    monkeypatch.setattr(debparse, "MAX_CONTROL_TEXT_BYTES", 1024)
    with pytest.raises(DebParseError, match="control 文件超过大小限制"):
        parse_deb(deb)


def test_zstd_window_memory_is_bounded(monkeypatch):
    import zstandard
    from prvaptmirror import debparse
    blob = zstandard.ZstdCompressor(write_content_size=False).compress(b"x" * (1024 * 1024))
    monkeypatch.setattr(debparse, "MAX_DECODER_MEMORY", 64 * 1024)
    with pytest.raises(DebParseError):
        debparse._decompress_tar("control.tar.zst", blob)


def test_rejects_truncated_skipped_ar_member(tmp_path):
    deb = build_deb(tmp_path / "truncated.deb")
    deb.write_bytes(deb.read_bytes()[:-20])
    with pytest.raises(DebParseError, match="截断"):
        parse_deb(deb)


def test_duplicate_control_archives_cannot_accumulate_in_memory(tmp_path):
    from tests.deb_builder import _ar_member
    deb = build_deb(tmp_path / "duplicate-control.deb")
    with deb.open("ab") as stream:
        stream.write(_ar_member("control.tar", b"x" * 1024))
    with pytest.raises(DebParseError, match="重复的 control.tar"):
        parse_deb(deb)
