"""Row types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class User:
    id: int
    username: str
    password_hash: str
    must_change_password: bool
    created_at: str
    last_login_at: str | None


@dataclass
class PackageRow:
    id: int
    name: str
    version: str
    architecture: str
    component: str
    filename: str
    size: int
    md5: str
    sha1: str
    sha256: str
    control_json: str
    state: str
    uploaded_at: str
    uploaded_by: int | None

    @classmethod
    def from_row(cls, row: Any) -> "PackageRow":
        return cls(
            id=row["id"],
            name=row["name"],
            version=row["version"],
            architecture=row["architecture"],
            component=row["component"],
            filename=row["filename"],
            size=row["size"],
            md5=row["md5"],
            sha1=row["sha1"],
            sha256=row["sha256"],
            control_json=row["control_json"],
            state=row["state"],
            uploaded_at=row["uploaded_at"],
            uploaded_by=row["uploaded_by"],
        )


@dataclass
class PublishResult:
    ok: bool
    error: str | None = None
    skipped: list[PackageRow] | None = None
    package_count: int = 0
    duration_ms: int = 0
    dists_inode: int | None = None
