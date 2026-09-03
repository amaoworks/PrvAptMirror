from __future__ import annotations

import httpx
import pytest

from prvaptmirror.db import connect, init_db
from prvaptmirror.source_sync import SourceScheduler, recover_interrupted_source_runs, sync_source_once
from prvaptmirror.sources import (
    SourceValidationError,
    create_source,
    delete_source,
    decrypt_token,
    get_source,
    queue_source,
    update_source,
)
from tests.deb_builder import build_deb


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _values(**overrides: str) -> dict[str, str]:
    values = {
        "name": "Example releases",
        "kind": "github_release",
        "location": "https://github.com/acme/tool/releases",
        "asset_pattern": r"tool_.*_(amd64|all)\.deb$",
        "interval_minutes": "30",
        "release_limit": "2",
        "include_prereleases": "",
        "enabled": "yes",
    }
    values.update(overrides)
    return values


def test_source_crud_normalizes_and_encrypts_token(cfg):
    conn = init_db(cfg)
    source = create_source(cfg, conn, _values(), "github-secret-token")
    assert source.location == "acme/tool"
    assert source.token_encrypted
    assert "github-secret-token" not in source.token_encrypted
    assert decrypt_token(cfg, source.token_encrypted) == "github-secret-token"

    updated = update_source(
        cfg,
        conn,
        source.id,
        _values(name="Renamed", interval_minutes="60"),
    )
    assert updated.name == "Renamed"
    assert updated.interval_minutes == 60
    assert updated.token_encrypted == source.token_encrypted

    cleared = update_source(cfg, conn, source.id, _values(), clear_token=True)
    assert cleared.token_encrypted is None
    conn.close()


def test_source_validation_rejects_bad_values(cfg):
    conn = init_db(cfg)
    try:
        create_source(cfg, conn, _values(location="https://gitlab.com/acme/tool"))
    except SourceValidationError as exc:
        assert "github.com" in str(exc)
    else:
        raise AssertionError("non-GitHub repository was accepted")
    try:
        create_source(cfg, conn, _values(name="Bad regex", asset_pattern="["))
    except SourceValidationError as exc:
        assert "表达式无效" in str(exc)
    else:
        raise AssertionError("invalid regex was accepted")
    conn.close()


def test_github_release_sync_imports_once_and_publishes(ready, tmp_path):
    deb = build_deb(
        tmp_path / "tool_2.0-1_amd64.deb",
        package="release-tool",
        version="2.0-1",
        architecture="amd64",
    ).read_bytes()
    common_deb = build_deb(
        tmp_path / "tool_2.0-1_all.deb",
        package="release-tool-common",
        version="2.0-1",
        architecture="all",
    ).read_bytes()
    asset_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal asset_requests
        if request.url.path == "/repos/acme/tool/releases":
            assert request.headers["authorization"] == "Bearer github-secret-token"
            releases = [
                {
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "id": 101,
                            "name": "tool_2.0-1_amd64.deb",
                            "url": "https://api.github.com/repos/acme/tool/releases/assets/101",
                            "browser_download_url": "https://github.com/acme/tool/releases/download/v2/tool.deb",
                            "size": len(deb),
                            "updated_at": "2026-09-02T00:00:00Z",
                        },
                        {
                            "id": 102,
                            "name": "checksums.txt",
                            "url": "https://api.github.com/repos/acme/tool/releases/assets/102",
                            "size": 10,
                            "updated_at": "2026-09-02T00:00:00Z",
                        },
                        {
                            "id": 103,
                            "name": "tool_2.0-1_all.deb",
                            "url": "https://api.github.com/repos/acme/tool/releases/assets/103",
                            "size": len(common_deb),
                            "updated_at": "2026-09-02T00:00:00Z",
                        },
                    ],
                },
                {"draft": False, "prerelease": True, "assets": []},
            ]
            return httpx.Response(200, json=releases)
        if request.url.path.endswith("/assets/101"):
            asset_requests += 1
            assert request.headers["accept"] == "application/octet-stream"
            return httpx.Response(200, content=deb)
        if request.url.path.endswith("/assets/103"):
            asset_requests += 1
            return httpx.Response(200, content=common_deb)
        return httpx.Response(404, json={"message": "not found"})

    conn = connect(ready)
    source = create_source(ready, conn, _values(release_limit="1"), "github-secret-token")
    conn.close()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert sync_source_once(ready, source.id, client=client)

    conn = connect(ready)
    package = conn.execute("SELECT * FROM packages WHERE name = 'release-tool'").fetchone()
    assert package is not None
    assert conn.execute("SELECT count(*) FROM packages").fetchone()[0] == 2
    saved = get_source(conn, source.id)
    assert saved is not None and saved.last_status == "success"
    artifacts = conn.execute(
        "SELECT * FROM source_artifacts WHERE source_id = ?", (source.id,)
    ).fetchall()
    assert [artifact["status"] for artifact in artifacts] == ["imported", "imported"]
    run = conn.execute(
        "SELECT * FROM source_runs WHERE source_id = ? ORDER BY id DESC", (source.id,)
    ).fetchone()
    assert (run["discovered"], run["downloaded"], run["imported"]) == (2, 2, 2)
    assert conn.execute("SELECT count(*) FROM publish_runs").fetchone()[0] == 1
    assert not sync_source_once(ready, client=client)
    assert queue_source(conn, source.id)
    conn.close()
    assert (ready.dists_dir / ready.suite / "InRelease").is_file()

    assert sync_source_once(ready, client=client)
    assert asset_requests == 2
    conn = connect(ready)
    second = conn.execute(
        "SELECT * FROM source_runs WHERE source_id = ? ORDER BY id DESC", (source.id,)
    ).fetchone()
    assert second["skipped"] == 2
    assert conn.execute("SELECT count(*) FROM publish_runs").fetchone()[0] == 1
    assert delete_source(conn, source.id)
    assert conn.execute("SELECT count(*) FROM packages WHERE name='release-tool'").fetchone()[0] == 1
    conn.close()
    client.close()


def test_direct_url_uses_conditional_request_and_skips_same_content(ready, tmp_path):
    deb = build_deb(
        tmp_path / "direct_1.0-1_all.deb",
        package="direct-tool",
        version="1.0-1",
        architecture="all",
    ).read_bytes()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            assert "if-none-match" not in request.headers
            return httpx.Response(
                200,
                content=deb,
                headers={
                    "ETag": '"direct-v1"',
                    "Content-Disposition": 'attachment; filename="direct_1.0-1_all.deb"',
                },
            )
        assert request.headers["if-none-match"] == '"direct-v1"'
        return httpx.Response(304)

    conn = connect(ready)
    source = create_source(
        ready,
        conn,
        _values(
            name="Direct package",
            kind="direct_url",
            location="https://downloads.example.test/current",
            asset_pattern=r".*\.deb$",
        ),
    )
    conn.close()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert sync_source_once(ready, source.id, client=client)
    conn = connect(ready)
    assert conn.execute("SELECT count(*) FROM packages WHERE name='direct-tool'").fetchone()[0] == 1
    saved = get_source(conn, source.id)
    assert saved is not None and saved.http_etag == '"direct-v1"'
    assert queue_source(conn, source.id)
    conn.close()
    assert sync_source_once(ready, source.id, client=client)
    conn = connect(ready)
    last = conn.execute(
        "SELECT * FROM source_runs WHERE source_id=? ORDER BY id DESC", (source.id,)
    ).fetchone()
    assert last["status"] == "success"
    assert last["downloaded"] == 0
    conn.close()
    client.close()


def test_failed_sync_records_error_and_short_retry(ready):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "unavailable"})

    conn = connect(ready)
    source = create_source(ready, conn, _values())
    conn.close()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert sync_source_once(ready, source.id, client=client)
    conn = connect(ready)
    failed = get_source(conn, source.id)
    assert failed is not None
    assert failed.last_status == "error"
    assert failed.consecutive_failures == 1
    assert "503" in (failed.last_error or "")
    run = conn.execute(
        "SELECT * FROM source_runs WHERE source_id=? ORDER BY id DESC", (source.id,)
    ).fetchone()
    assert run["status"] == "error"
    conn.close()
    client.close()


def test_recover_interrupted_source_run(cfg):
    conn = init_db(cfg)
    source = create_source(cfg, conn, _values())
    conn.execute(
        "UPDATE package_sources SET last_status='running' WHERE id=?", (source.id,)
    )
    conn.execute(
        "INSERT INTO source_runs(source_id, started_at, status) VALUES (?, 'now', 'running')",
        (source.id,),
    )
    recover_interrupted_source_runs(conn)
    recovered = get_source(conn, source.id)
    assert recovered is not None and recovered.last_status == "error"
    run = conn.execute("SELECT * FROM source_runs WHERE source_id=?", (source.id,)).fetchone()
    assert run["status"] == "error"
    assert "中断" in run["error"]
    conn.close()


@pytest.mark.anyio
async def test_in_process_scheduler_runs_immediately_and_wakes(cfg, monkeypatch):
    calls = 0
    conn = init_db(cfg)
    conn.close()

    def fake_run_due(_cfg):
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr("prvaptmirror.source_sync.run_due_sources", fake_run_due)
    scheduler = SourceScheduler(cfg, poll_seconds=3600)
    await scheduler.start()
    for _ in range(100):
        if calls >= 1:
            break
        await __import__("asyncio").sleep(0.01)
    assert calls == 1
    scheduler.wake()
    for _ in range(100):
        if calls >= 2:
            break
        await __import__("asyncio").sleep(0.01)
    assert calls == 2
    await scheduler.stop()
