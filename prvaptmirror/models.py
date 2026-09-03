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


@dataclass
class SourceRow:
    id: int
    name: str
    kind: str
    location: str
    asset_pattern: str
    interval_minutes: int
    release_limit: int
    include_prereleases: bool
    enabled: bool
    token_encrypted: str | None
    created_at: str
    updated_at: str
    next_check_at: str
    last_checked_at: str | None
    last_status: str
    last_error: str | None
    consecutive_failures: int
    http_etag: str | None
    http_last_modified: str | None

    @classmethod
    def from_row(cls, row: Any) -> "SourceRow":
        return cls(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            location=row["location"],
            asset_pattern=row["asset_pattern"],
            interval_minutes=row["interval_minutes"],
            release_limit=row["release_limit"],
            include_prereleases=bool(row["include_prereleases"]),
            enabled=bool(row["enabled"]),
            token_encrypted=row["token_encrypted"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            next_check_at=row["next_check_at"],
            last_checked_at=row["last_checked_at"],
            last_status=row["last_status"],
            last_error=row["last_error"],
            consecutive_failures=row["consecutive_failures"],
            http_etag=row["http_etag"],
            http_last_modified=row["http_last_modified"],
        )
