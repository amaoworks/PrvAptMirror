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
    )
    parsed = parse_deb(deb, allowed_archs=("amd64", "arm64", "all"))
    assert parsed.name == "zstpkg"


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
