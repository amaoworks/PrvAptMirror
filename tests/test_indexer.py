from datetime import datetime, timezone
from pathlib import Path

from prvaptmirror.indexer import rebuild_dists, render_packages, write_gzip_stable
from prvaptmirror.models import PackageRow


def _row(**kwargs) -> PackageRow:
    base = dict(
        id=1,
        name="hello-prv",
        version="1.0-1",
        architecture="all",
        component="main",
        filename="pool/main/h/hello-prv/hello-prv_1.0-1_all.deb",
        size=1234,
        md5="a" * 32,
        sha1="b" * 40,
        sha256="c" * 64,
        control_json='{"Package":"hello-prv","Version":"1.0-1","Architecture":"all","Description":"example arch-all package\\nThis is the extended description.\\n\\nSecond paragraph."}',
        state="active",
        uploaded_at="t",
        uploaded_by=None,
    )
    base.update(kwargs)
    return PackageRow(**base)


def test_empty_packages_is_zero_bytes():
    assert render_packages([]) == b""


def test_packages_field_order_and_all_arch_left_as_all():
    body = render_packages([_row()]).decode("utf-8")
    assert body.startswith("Package: hello-prv\n")
    assert "Architecture: all\n" in body
    assert "Filename: pool/main/h/hello-prv/hello-prv_1.0-1_all.deb\n" in body
    assert "MD5sum: " in body
    assert "Essential" not in body
    assert body.index("Package:") < body.index("Filename:")
    assert body.index("Description:") < body.index("Filename:")


def test_rebuild_merges_all_into_binary_amd64(cfg, tmp_path: Path):
    staging = tmp_path / "dists" / cfg.suite
    skipped = rebuild_dists(
        [_row()],
        staging,
        cfg,
        now=datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert skipped == []
    amd = (staging / "main" / "binary-amd64" / "Packages").read_bytes()
    allp = (staging / "main" / "binary-all" / "Packages").read_bytes()
    arm = (staging / "main" / "binary-arm64" / "Packages").read_bytes()
    assert b"Architecture: all" in amd
    assert b"hello-prv" in amd
    assert b"hello-prv" in allp
    assert b"hello-prv" in arm
    empty_ok = (staging / "main" / "binary-amd64" / "Packages").stat().st_size > 0
    assert empty_ok
    rel = (staging / "Release").read_text(encoding="utf-8")
    assert "Date: Wed, 19 Aug 2026 12:00:00 UTC" in rel
    assert "main/binary-all/Release" in rel
    assert "main/binary-amd64/Packages" in rel
    assert rel.index("main/binary-all/Packages") < rel.index("main/binary-amd64/Packages")


def test_gzip_stable_no_filename(tmp_path: Path):
    dest = tmp_path / "Packages.gz"
    write_gzip_stable(dest, b"abc")
    raw = dest.read_bytes()
    # gzip header flags: FNAME is bit 3 of byte 3
    flags = raw[3]
    assert flags & 0x08 == 0


def test_unknown_arch_skipped_not_raised(cfg, tmp_path: Path):
    staging = tmp_path / "dists" / cfg.suite
    skipped = rebuild_dists(
        [_row(architecture="i386", filename="pool/main/h/hello-prv/hello-prv_1.0-1_i386.deb")],
        staging,
        cfg,
    )
    assert len(skipped) == 1
    assert (staging / "main" / "binary-amd64" / "Packages").read_bytes() == b""
