"""In-process scheduler and remote .deb synchronization."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
from debian.debian_support import Version

from prvaptmirror import __version__
from prvaptmirror.config import Config
from prvaptmirror.db import connect, get_package_nva, transaction
from prvaptmirror.debparse import DebParseError, ParsedDeb, parse_deb
from prvaptmirror.events import emit
from prvaptmirror.models import SourceRow
from prvaptmirror.publish import retry_pending_publish, upload_commit
from prvaptmirror.settings import load_app_config
from prvaptmirror.sources import (
    decrypt_token,
    normalize_direct_url,
    now_iso,
    source_from_row,
    validate_github_probe_values,
)
from prvaptmirror.storage import DiskFullError, write_incoming_stream

FINAL_ARTIFACT_STATUSES = {"imported", "skipped", "rejected", "conflict"}
MAX_DIRECTORY_INDEX_BYTES = 2 * 1024 * 1024
USER_AGENT = f"PrvAptMirror/{__version__}"


@dataclass(frozen=True)
class RemoteAsset:
    external_id: str
    filename: str
    url: str
    size: int | None = None


@dataclass(frozen=True)
class GitHubAssetPreview:
    release: str
    filename: str
    size: int | None
    is_deb: bool
    pattern_matches: bool

    @property
    def selected(self) -> bool:
        return self.is_deb and self.pattern_matches


@dataclass(frozen=True)
class GitHubSourcePreview:
    repository: str
    releases_checked: int
    assets: tuple[GitHubAssetPreview, ...]
    truncated: bool = False

    @property
    def matched_count(self) -> int:
        return sum(asset.selected for asset in self.assets)


@dataclass(frozen=True)
class DirectoryAssetPreview:
    filename: str
    version: str | None
    architecture: str | None
    is_deb: bool
    pattern_matches: bool
    valid_filename: bool
    selected: bool
    response_status: int | None = None
    content_type: str | None = None
    valid_deb: bool | None = None
    error: str | None = None


@dataclass(frozen=True)
class DirectorySourcePreview:
    directory: str
    assets: tuple[DirectoryAssetPreview, ...]
    truncated: bool = False

    @property
    def matched_count(self) -> int:
        return sum(asset.selected for asset in self.assets)

    @property
    def ready_count(self) -> int:
        return sum(asset.selected and asset.valid_deb is True for asset in self.assets)


@dataclass(frozen=True)
class _DirectoryCandidate:
    filename: str
    url: str
    is_deb: bool
    pattern_matches: bool
    identity: tuple[str, Version, str] | None


@dataclass
class SyncStats:
    discovered: int = 0
    downloaded: int = 0
    imported: int = 0
    skipped: int = 0


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)
                return


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:2000]


def recover_interrupted_source_runs(conn: sqlite3.Connection) -> None:
    now = now_iso()
    conn.execute(
        """
        UPDATE source_runs SET finished_at = ?, status = 'error', error = '应用重启，任务已中断'
        WHERE status = 'running'
        """,
        (now,),
    )
    conn.execute(
        """
        UPDATE package_sources
        SET last_status = 'error', last_error = '应用重启，任务已中断', next_check_at = ?
        WHERE last_status = 'running'
        """,
        (now,),
    )


def _claim_source(
    conn: sqlite3.Connection, source_id: int | None = None
) -> tuple[SourceRow, int] | None:
    now = now_iso()
    with transaction(conn):
        if source_id is None:
            row = conn.execute(
                """
                SELECT * FROM package_sources
                WHERE enabled = 1 AND last_status != 'running' AND next_check_at <= ?
                ORDER BY next_check_at, id LIMIT 1
                """,
                (now,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM package_sources
                WHERE id = ? AND enabled = 1 AND last_status != 'running'
                """,
                (source_id,),
            ).fetchone()
        if row is None:
            return None
        source = source_from_row(row)
        conn.execute(
            "UPDATE package_sources SET last_status = 'running', last_error = NULL WHERE id = ?",
            (source.id,),
        )
        cur = conn.execute(
            "INSERT INTO source_runs (source_id, started_at, status) VALUES (?, ?, 'running')",
            (source.id, now),
        )
        return source, int(cur.lastrowid)


def _finish_source(
    conn: sqlite3.Connection,
    source: SourceRow,
    run_id: int,
    stats: SyncStats,
    error: str | None,
) -> None:
    now = _utc_now()
    current = conn.execute(
        "SELECT interval_minutes, consecutive_failures FROM package_sources WHERE id = ?",
        (source.id,),
    ).fetchone()
    if current is None:
        return
    interval_minutes = int(current["interval_minutes"])
    if error:
        failures = int(current["consecutive_failures"]) + 1
        retry_seconds = min(interval_minutes * 60, 60 * (2 ** min(failures - 1, 8)))
        status = "error"
    else:
        failures = 0
        retry_seconds = interval_minutes * 60
        status = "success"
    finished = _iso(now)
    next_check = _iso(now + timedelta(seconds=retry_seconds))
    with transaction(conn):
        conn.execute(
            """
            UPDATE source_runs SET finished_at = ?, status = ?, discovered = ?,
              downloaded = ?, imported = ?, skipped = ?, error = ? WHERE id = ?
            """,
            (
                finished,
                status,
                stats.discovered,
                stats.downloaded,
                stats.imported,
                stats.skipped,
                error,
                run_id,
            ),
        )
        conn.execute(
            """
            UPDATE package_sources SET last_checked_at = ?, last_status = ?, last_error = ?,
              consecutive_failures = ?, next_check_at = ? WHERE id = ?
            """,
            (finished, status, error, failures, next_check, source.id),
        )


def _github_release_items(
    client: httpx.Client,
    repository: str,
    *,
    include_prereleases: bool,
    release_limit: int,
) -> list[dict]:
    response = client.get(
        f"https://api.github.com/repos/{repository}/releases",
        params={"per_page": 20},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("GitHub Releases API 返回了无效内容")
    releases = [
        item
        for item in payload
        if isinstance(item, dict)
        and not item.get("draft")
        and (include_prereleases or not item.get("prerelease"))
    ][:release_limit]
    return releases


def _github_assets(client: httpx.Client, source: SourceRow) -> list[RemoteAsset]:
    releases = _github_release_items(
        client,
        source.location,
        include_prereleases=source.include_prereleases,
        release_limit=source.release_limit,
    )
    pattern = re.compile(source.asset_pattern, re.IGNORECASE)
    found: list[RemoteAsset] = []
    for release in reversed(releases):
        for asset in release.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            url = str(asset.get("url") or asset.get("browser_download_url") or "")
            asset_id = asset.get("id")
            if (
                not name.lower().endswith(".deb")
                or not pattern.search(name)
                or not url.startswith(("https://", "http://"))
                or asset_id is None
            ):
                continue
            updated = str(asset.get("updated_at") or "")
            size = asset.get("size")
            found.append(
                RemoteAsset(
                    external_id=f"github:{asset_id}:{updated}",
                    filename=name[:500],
                    url=url,
                    size=int(size) if isinstance(size, int) else None,
                )
            )
    return found


def _directory_deb_identity(filename: str) -> tuple[str, Version, str] | None:
    if not filename.lower().endswith(".deb"):
        return None
    try:
        package, version_text, architecture = filename[:-4].rsplit("_", 2)
    except ValueError:
        return None
    if not package or not version_text or not architecture:
        return None
    try:
        version = Version(version_text)
    except Exception:
        return None
    return package.lower(), version, architecture.lower()


def _http_directory_candidates(
    client: httpx.Client,
    location: str,
    asset_pattern: str,
) -> list[_DirectoryCandidate]:
    response = client.get(
        location,
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    response.raise_for_status()
    if len(response.content) > MAX_DIRECTORY_INDEX_BYTES:
        raise ValueError("HTTP 目录索引超过 2 MiB 限制")

    parser = _HrefParser()
    parser.feed(response.text)
    pattern = re.compile(asset_pattern, re.IGNORECASE)
    base = urlparse(location)
    base_path = base.path if base.path.endswith("/") else base.path + "/"
    candidates: list[_DirectoryCandidate] = []

    for href in parser.hrefs:
        candidate_url = urljoin(location, href)
        candidate = urlparse(candidate_url)
        if candidate.scheme != base.scheme or candidate.netloc != base.netloc:
            continue
        candidate_parent = candidate.path.rsplit("/", 1)[0] + "/"
        if candidate_parent != base_path:
            continue
        filename = unquote(candidate.path.rsplit("/", 1)[-1])
        if not filename:
            continue
        identity = _directory_deb_identity(filename)
        candidates.append(
            _DirectoryCandidate(
                filename=filename[:500],
                url=candidate_url,
                is_deb=filename.lower().endswith(".deb"),
                pattern_matches=bool(pattern.search(filename)),
                identity=identity,
            )
        )
    return candidates


def _latest_directory_candidates(
    candidates: list[_DirectoryCandidate],
) -> list[_DirectoryCandidate]:
    latest: dict[tuple[str, str], tuple[Version, _DirectoryCandidate]] = {}
    for candidate in candidates:
        if not candidate.pattern_matches or candidate.identity is None:
            continue
        package, version, architecture = candidate.identity
        key = (package, architecture)
        current = latest.get(key)
        if current is None or current[0] < version:
            latest[key] = (version, candidate)
    return [item[1] for _, item in sorted(latest.items())]


def _http_directory_assets(
    client: httpx.Client, source: SourceRow
) -> list[RemoteAsset]:
    candidates = _http_directory_candidates(
        client, source.location, source.asset_pattern
    )
    return [
        RemoteAsset(
            external_id=f"directory:{candidate.url}",
            filename=candidate.filename,
            url=candidate.url,
        )
        for candidate in _latest_directory_candidates(candidates)
    ]


def _probe_deb_response(
    client: httpx.Client, url: str
) -> tuple[int | None, str | None, bool, str | None]:
    try:
        with client.stream(
            "GET",
            url,
            headers={
                "Accept": "application/octet-stream",
                "Range": "bytes=0-7",
            },
        ) as response:
            response.raise_for_status()
            prefix = bytearray()
            for chunk in response.iter_bytes():
                prefix.extend(chunk)
                if len(prefix) >= 8:
                    break
            content_type = response.headers.get("content-type")
            if bytes(prefix[:8]) != b"!<arch>\n":
                actual = bytes(prefix[:8]).hex(" ") or "（空响应）"
                return (
                    response.status_code,
                    content_type,
                    False,
                    f"响应开头为 {actual}，不是 Debian ar 文件",
                )
            return response.status_code, content_type, True, None
    except httpx.HTTPStatusError as exc:
        return (
            exc.response.status_code,
            exc.response.headers.get("content-type"),
            False,
            str(exc),
        )
    except httpx.RequestError as exc:
        return None, None, False, str(exc)


def preview_http_directory(
    location: str,
    asset_pattern: str,
    *,
    client: httpx.Client | None = None,
    max_assets: int = 200,
) -> DirectorySourcePreview:
    """Preview directory matches and verify selected URLs return real .deb data."""
    directory = normalize_direct_url(location)
    if not urlparse(directory).path.endswith("/"):
        raise ValueError("HTTP 目录地址必须以 / 结尾")
    pattern_text = asset_pattern.strip() or r".*\.deb$"
    try:
        re.compile(pattern_text, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Asset 匹配表达式无效：{exc}") from exc

    owned_client = client is None
    if client is None:
        client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    try:
        candidates = _http_directory_candidates(client, directory, pattern_text)
        selected_urls = {
            candidate.url for candidate in _latest_directory_candidates(candidates)
        }
        previews: list[DirectoryAssetPreview] = []
        for candidate in candidates[:max_assets]:
            selected = candidate.url in selected_urls
            status: int | None = None
            content_type: str | None = None
            valid_deb: bool | None = None
            error: str | None = None
            if selected:
                status, content_type, valid_deb, error = _probe_deb_response(
                    client, candidate.url
                )
            identity = candidate.identity
            previews.append(
                DirectoryAssetPreview(
                    filename=candidate.filename,
                    version=str(identity[1]) if identity else None,
                    architecture=identity[2] if identity else None,
                    is_deb=candidate.is_deb,
                    pattern_matches=candidate.pattern_matches,
                    valid_filename=identity is not None,
                    selected=selected,
                    response_status=status,
                    content_type=content_type,
                    valid_deb=valid_deb,
                    error=error,
                )
            )
        return DirectorySourcePreview(
            directory=directory,
            assets=tuple(previews),
            truncated=len(candidates) > max_assets,
        )
    finally:
        if owned_client:
            client.close()


def preview_github_source(
    location: str,
    asset_pattern: str,
    release_limit: str,
    *,
    include_prereleases: bool = False,
    token: str | None = None,
    client: httpx.Client | None = None,
    max_assets: int = 200,
) -> GitHubSourcePreview:
    """Read GitHub metadata and show which assets a source would select."""
    repository, pattern_text, limit = validate_github_probe_values(
        location, asset_pattern, release_limit
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    owned_client = client is None
    if client is None:
        client = httpx.Client(
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    else:
        client.headers.update(headers)
    try:
        releases = _github_release_items(
            client,
            repository,
            include_prereleases=include_prereleases,
            release_limit=limit,
        )
        pattern = re.compile(pattern_text, re.IGNORECASE)
        previews: list[GitHubAssetPreview] = []
        truncated = False
        for release in releases:
            release_name = str(
                release.get("tag_name") or release.get("name") or release.get("id") or "—"
            )
            for asset in release.get("assets") or []:
                if not isinstance(asset, dict):
                    continue
                if len(previews) >= max_assets:
                    truncated = True
                    break
                filename = str(asset.get("name") or "")[:500]
                size = asset.get("size")
                previews.append(
                    GitHubAssetPreview(
                        release=release_name[:200],
                        filename=filename,
                        size=int(size) if isinstance(size, int) else None,
                        is_deb=filename.lower().endswith(".deb"),
                        pattern_matches=bool(pattern.search(filename)),
                    )
                )
            if truncated:
                break
        return GitHubSourcePreview(
            repository=repository,
            releases_checked=len(releases),
            assets=tuple(previews),
            truncated=truncated,
        )
    finally:
        if owned_client:
            client.close()


def _filename_from_response(response: httpx.Response, fallback_url: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.IGNORECASE)
    if match:
        name = unquote(match.group(1).strip())
    else:
        name = unquote(Path(urlparse(str(response.url or fallback_url)).path).name)
    return (name or "download.deb")[:500]


def _download(
    client: httpx.Client,
    cfg: Config,
    asset: RemoteAsset,
    *,
    conditional_headers: dict[str, str] | None = None,
) -> tuple[Path | None, httpx.Response]:
    headers = {"Accept": "application/octet-stream", **(conditional_headers or {})}
    with client.stream("GET", asset.url, headers=headers) as response:
        if response.status_code == 304:
            return None, response
        response.raise_for_status()
        length = response.headers.get("content-length")
        if length and int(length) > cfg.max_upload_bytes:
            raise ValueError("远端软件包超过网站设置的单文件大小限制")

        def validated_chunks():
            prefix = bytearray()
            validated = False
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if not validated:
                    prefix.extend(chunk)
                    if len(prefix) < 8:
                        continue
                    if bytes(prefix[:8]) != b"!<arch>\n":
                        content_type = response.headers.get("content-type", "未知")
                        actual = bytes(prefix[:8]).hex(" ")
                        raise ValueError(
                            "下载响应不是 .deb"
                            f"（HTTP {response.status_code}，Content-Type {content_type}，"
                            f"开头 {actual}）"
                        )
                    validated = True
                    yield bytes(prefix)
                    prefix.clear()
                    continue
                yield chunk
            if not validated:
                content_type = response.headers.get("content-type", "未知")
                raise ValueError(
                    "下载响应不是 .deb"
                    f"（HTTP {response.status_code}，Content-Type {content_type}，响应过短）"
                )

        path = write_incoming_stream(
            cfg,
            validated_chunks(),
            limit=cfg.max_upload_bytes,
        )
        return path, response


def _artifact_row(conn: sqlite3.Connection, source_id: int, external_id: str):
    return conn.execute(
        "SELECT * FROM source_artifacts WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()


def _begin_artifact(
    conn: sqlite3.Connection, source: SourceRow, asset: RemoteAsset
) -> tuple[int, str | None]:
    existing = _artifact_row(conn, source.id, asset.external_id)
    if existing is not None and existing["status"] in FINAL_ARTIFACT_STATUSES:
        manually_queued = source.last_status == "queued"
        retryable_status = existing["status"] in {"rejected", "conflict"}
        if not (manually_queued and retryable_status):
            return int(existing["id"]), str(existing["status"])
    now = now_iso()
    conn.execute(
        """
        INSERT INTO source_artifacts (
          source_id, external_id, filename, remote_url, size, status, first_seen_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(source_id, external_id) DO UPDATE SET
          filename = excluded.filename, remote_url = excluded.remote_url,
          size = excluded.size, status = 'pending', finished_at = NULL, error = NULL
        """,
        (source.id, asset.external_id, asset.filename, asset.url, asset.size, now),
    )
    row = _artifact_row(conn, source.id, asset.external_id)
    assert row is not None
    return int(row["id"]), None


def _finish_artifact(
    conn: sqlite3.Connection,
    artifact_id: int,
    status: str,
    *,
    parsed: ParsedDeb | None = None,
    package_id: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE source_artifacts SET status = ?, size = COALESCE(?, size),
          sha256 = COALESCE(?, sha256), package_id = ?, finished_at = ?, error = ?
        WHERE id = ?
        """,
        (
            status,
            parsed.size if parsed else None,
            parsed.sha256 if parsed else None,
            package_id,
            now_iso(),
            error,
            artifact_id,
        ),
    )


def _process_assets(
    client: httpx.Client,
    cfg: Config,
    source: SourceRow,
    conn: sqlite3.Connection,
    assets: list[RemoteAsset],
    stats: SyncStats,
) -> list[str]:
    pending: list[tuple[ParsedDeb, Path]] = []
    metadata: list[tuple[int, ParsedDeb]] = []
    errors: list[str] = []
    stats.discovered += len(assets)
    for asset in assets:
        artifact_id, final_status = _begin_artifact(conn, source, asset)
        if final_status:
            stats.skipped += 1
            continue
        incoming: Path | None = None
        try:
            if asset.size is not None and asset.size > cfg.max_upload_bytes:
                raise ValueError("远端软件包超过网站设置的单文件大小限制")
            incoming, _response = _download(client, cfg, asset)
            assert incoming is not None
            stats.downloaded += 1
            parsed = parse_deb(incoming, allowed_archs=cfg.architectures)
            pending.append((parsed, incoming))
            metadata.append((artifact_id, parsed))
        except (DebParseError, ValueError, DiskFullError) as exc:
            if incoming is not None:
                incoming.unlink(missing_ok=True)
            message = _safe_error(exc)
            _finish_artifact(conn, artifact_id, "rejected", error=message)
            stats.skipped += 1
            errors.append(f"{asset.filename}: {message}")
        except Exception as exc:
            if incoming is not None:
                incoming.unlink(missing_ok=True)
            message = _safe_error(exc)
            _finish_artifact(conn, artifact_id, "failed", error=message)
            errors.append(f"{asset.filename}: {message}")

    if not pending:
        return errors
    results, pub = upload_commit(cfg, conn, pending, user_id=None)
    for result, (artifact_id, parsed) in zip(results, metadata, strict=True):
        if result["ok"]:
            _finish_artifact(
                conn, artifact_id, "imported", parsed=parsed, package_id=int(result["id"])
            )
            stats.imported += 1
            continue
        existing = get_package_nva(conn, parsed.name, parsed.version, parsed.architecture)
        if existing is not None and existing.sha256 == parsed.sha256:
            _finish_artifact(
                conn, artifact_id, "skipped", parsed=parsed, package_id=existing.id
            )
            stats.skipped += 1
        else:
            message = "相同 Package、Version 和 Architecture 已存在，但文件摘要不同"
            _finish_artifact(conn, artifact_id, "conflict", parsed=parsed, error=message)
            stats.skipped += 1
            errors.append(f"{parsed.name}: {message}")
    if pub is not None and not pub.ok:
        errors.append(f"软件包已导入，但仓库索引发布失败：{pub.error}")
    return errors


def _sync_direct(
    client: httpx.Client,
    cfg: Config,
    source: SourceRow,
    conn: sqlite3.Connection,
    stats: SyncStats,
) -> list[str]:
    headers: dict[str, str] = {}
    if source.http_etag:
        headers["If-None-Match"] = source.http_etag
    if source.http_last_modified:
        headers["If-Modified-Since"] = source.http_last_modified
    placeholder = RemoteAsset("direct:pending", "download.deb", source.location)
    incoming: Path | None = None
    try:
        incoming, response = _download(client, cfg, placeholder, conditional_headers=headers)
        if incoming is None:
            return []
        stats.discovered += 1
        stats.downloaded += 1
        parsed = parse_deb(incoming, allowed_archs=cfg.architectures)
        digest = parsed.sha256
        asset = RemoteAsset(
            external_id=f"direct:{digest}",
            filename=_filename_from_response(response, source.location),
            url=source.location,
            size=parsed.size,
        )
        artifact_id, final_status = _begin_artifact(conn, source, asset)
        if final_status:
            incoming.unlink(missing_ok=True)
            stats.skipped += 1
        else:
            errors = _process_prepared_direct(cfg, conn, artifact_id, parsed, incoming, stats)
            if errors:
                return errors
        conn.execute(
            "UPDATE package_sources SET http_etag = ?, http_last_modified = ? WHERE id = ?",
            (response.headers.get("etag"), response.headers.get("last-modified"), source.id),
        )
        return []
    except (DebParseError, ValueError, DiskFullError) as exc:
        if incoming is not None:
            incoming.unlink(missing_ok=True)
        return [_safe_error(exc)]
    except Exception:
        if incoming is not None:
            incoming.unlink(missing_ok=True)
        raise


def _process_prepared_direct(
    cfg: Config,
    conn: sqlite3.Connection,
    artifact_id: int,
    parsed: ParsedDeb,
    incoming: Path,
    stats: SyncStats,
) -> list[str]:
    results, pub = upload_commit(cfg, conn, [(parsed, incoming)], user_id=None)
    result = results[0]
    if result["ok"]:
        _finish_artifact(
            conn, artifact_id, "imported", parsed=parsed, package_id=int(result["id"])
        )
        stats.imported += 1
    else:
        existing = get_package_nva(conn, parsed.name, parsed.version, parsed.architecture)
        if existing is not None and existing.sha256 == parsed.sha256:
            _finish_artifact(conn, artifact_id, "skipped", parsed=parsed, package_id=existing.id)
            stats.skipped += 1
        else:
            message = "相同 Package、Version 和 Architecture 已存在，但文件摘要不同"
            _finish_artifact(conn, artifact_id, "conflict", parsed=parsed, error=message)
            stats.skipped += 1
            return [message]
    if pub is not None and not pub.ok:
        return [f"软件包已导入，但仓库索引发布失败：{pub.error}"]
    return []


def _execute_source(
    base_cfg: Config,
    source: SourceRow,
    conn: sqlite3.Connection,
    client: httpx.Client,
) -> tuple[SyncStats, str | None]:
    stats = SyncStats()
    recovery = retry_pending_publish(base_cfg, conn)
    if recovery is not None and not recovery.ok:
        return stats, f"仓库索引重试发布失败：{recovery.error}"
    cfg = load_app_config(base_cfg, conn)
    if source.kind == "github_release":
        assets = _github_assets(client, source)
        errors = _process_assets(client, cfg, source, conn, assets, stats)
    elif source.kind == "direct_url":
        if urlparse(source.location).path.endswith("/"):
            assets = _http_directory_assets(client, source)
            errors = _process_assets(client, cfg, source, conn, assets, stats)
        else:
            errors = _sync_direct(client, cfg, source, conn, stats)
    else:
        raise RuntimeError(f"不支持的软件来源类型：{source.kind}")
    return stats, "; ".join(errors)[:2000] or None


def sync_source_once(
    base_cfg: Config,
    source_id: int | None = None,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Claim and execute one source. Returns False when nothing was claimable."""
    conn = connect(base_cfg)
    owned_client = client is None
    source: SourceRow | None = None
    run_id: int | None = None
    stats = SyncStats()
    try:
        claimed = _claim_source(conn, source_id)
        if claimed is None:
            return False
        source, run_id = claimed
        error: str | None = None
        try:
            token = decrypt_token(base_cfg, source.token_encrypted)
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if client is None:
                client = httpx.Client(
                    headers=headers,
                    follow_redirects=True,
                    timeout=httpx.Timeout(120.0, connect=20.0),
                )
            else:
                client.headers.update(headers)
            stats, error = _execute_source(base_cfg, source, conn, client)
        except Exception as exc:
            error = _safe_error(exc)
        _finish_source(conn, source, run_id, stats, error)
        emit(
            "source_sync",
            source=source.name,
            status="error" if error else "success",
            discovered=stats.discovered,
            downloaded=stats.downloaded,
            imported=stats.imported,
            skipped=stats.skipped,
            error=error,
        )
        return True
    finally:
        try:
            if owned_client and client is not None:
                client.close()
        finally:
            conn.close()


def run_due_sources(base_cfg: Config, *, max_sources: int = 10) -> int:
    count = 0
    while count < max_sources and sync_source_once(base_cfg):
        count += 1
    if count == 0:
        conn = connect(base_cfg)
        try:
            retry_pending_publish(base_cfg, conn)
        finally:
            conn.close()
    return count


class SourceScheduler:
    """One scheduler task inside the single Uvicorn process."""

    def __init__(self, base_cfg: Config, poll_seconds: float = 30.0) -> None:
        self.base_cfg = base_cfg
        self.poll_seconds = poll_seconds
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        conn = connect(self.base_cfg)
        try:
            recover_interrupted_source_runs(conn)
        finally:
            conn.close()
        self._task = asyncio.create_task(self._run(), name="package-source-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await asyncio.to_thread(run_due_sources, self.base_cfg)
            except Exception as exc:
                emit("source_scheduler_error", error=_safe_error(exc))
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
