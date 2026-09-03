"""In-process scheduler and remote .deb synchronization."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from prvaptmirror.config import Config
from prvaptmirror.db import connect, get_package_nva, transaction
from prvaptmirror.debparse import DebParseError, ParsedDeb, parse_deb
from prvaptmirror.events import emit
from prvaptmirror.models import SourceRow
from prvaptmirror.publish import upload_commit
from prvaptmirror.settings import load_app_config
from prvaptmirror.sources import decrypt_token, now_iso, source_from_row
from prvaptmirror.storage import DiskFullError, write_incoming_stream

FINAL_ARTIFACT_STATUSES = {"imported", "skipped", "rejected", "conflict"}


@dataclass(frozen=True)
class RemoteAsset:
    external_id: str
    filename: str
    url: str
    size: int | None = None


@dataclass
class SyncStats:
    discovered: int = 0
    downloaded: int = 0
    imported: int = 0
    skipped: int = 0


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


def _github_assets(client: httpx.Client, source: SourceRow) -> list[RemoteAsset]:
    response = client.get(
        f"https://api.github.com/repos/{source.location}/releases",
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
        and (source.include_prereleases or not item.get("prerelease"))
    ][: source.release_limit]
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
        path = write_incoming_stream(
            cfg,
            response.iter_bytes(chunk_size=1024 * 1024),
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
    cfg = load_app_config(base_cfg, conn)
    stats = SyncStats()
    if source.kind == "github_release":
        assets = _github_assets(client, source)
        errors = _process_assets(client, cfg, source, conn, assets, stats)
    elif source.kind == "direct_url":
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
        token = decrypt_token(base_cfg, source.token_encrypted)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "PrvAptMirror/0.0.2",
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
        error: str | None = None
        try:
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
        if owned_client and client is not None:
            client.close()
        conn.close()


def run_due_sources(base_cfg: Config, *, max_sources: int = 10) -> int:
    count = 0
    while count < max_sources and sync_source_once(base_cfg):
        count += 1
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
