from __future__ import annotations

import httpx
import pytest

from prvaptmirror.db import connect, init_db
from prvaptmirror.source_sync import (
    SourceScheduler,
    preview_github_source,
    preview_http_directory,
    recover_interrupted_source_runs,
    sync_source_once,
)
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


@pytest.mark.parametrize("kind", ["github_release", "direct_url", "directory"])
def test_sync_retries_failed_publish_without_reimport(ready, tmp_path, monkeypatch, kind):
    from prvaptmirror import publish as publishing
    from prvaptmirror.db import get_setting

    deb = build_deb(tmp_path / "tool_1_all.deb", package="retry-tool").read_bytes()
    downloads = 0

    def handler(request):
        nonlocal downloads
        if request.url.path == "/repos/acme/tool/releases":
            return httpx.Response(200, json=[{"assets": [{
                "id": 1, "name": "tool_1_all.deb", "size": len(deb),
                "url": "https://api.github.com/repos/acme/tool/releases/assets/1",
                "updated_at": "2026-09-20T00:00:00Z",
            }]}])
        if request.url.path == "/pool/":
            return httpx.Response(200, text='<a href="tool_1_all.deb">package</a>')
        downloads += 1
        return httpx.Response(200, content=deb)

    values = _values()
    if kind != "github_release":
        values.update(kind="direct_url", location="https://example.test/pool/" +
                      ("tool_1_all.deb" if kind == "direct_url" else ""))
    conn = connect(ready)
    source = create_source(ready, conn, values)
    original_sign = publishing.sign_release

    def fail_sign(*args):
        raise OSError("test signing unavailable")

    monkeypatch.setattr(publishing, "sign_release", fail_sign)
    with httpx.Client(transport=httpx.MockTransport(handler)) as remote:
        assert sync_source_once(ready, source.id, client=remote)
        assert get_source(conn, source.id).last_status == "error"
        assert get_setting(conn, "publish_dirty") == "1"
        assert conn.execute("SELECT status FROM source_artifacts").fetchone()[0] == "imported"
        assert queue_source(conn, source.id)
        # A second failure must remain an error, even with no new remote assets.
        assert sync_source_once(ready, source.id, client=remote)
        assert get_source(conn, source.id).last_status == "error"
        monkeypatch.setattr(publishing, "sign_release", original_sign)
        assert queue_source(conn, source.id)
        assert sync_source_once(ready, source.id, client=remote)
    assert get_source(conn, source.id).last_status == "success"
    assert get_setting(conn, "publish_dirty") == "0"
    assert conn.execute("SELECT count(*) FROM packages").fetchone()[0] == 1
    assert "Package: retry-tool" in (ready.dists_dir / "stable/main/binary-amd64/Packages").read_text()
    if kind != "direct_url":
        assert downloads == 1
    conn.close()


@pytest.mark.parametrize("failure", ["token", "client"])
def test_sync_initialization_failure_finishes_run_and_can_be_requeued(ready, monkeypatch, failure):
    conn = connect(ready)
    source = create_source(ready, conn, _values(), "synthetic-token")
    if failure == "token":
        conn.execute("UPDATE package_sources SET token_encrypted='invalid' WHERE id=?", (source.id,))
    else:
        def fail_client(**kwargs):
            raise ValueError("test client initialization failed")
        monkeypatch.setattr("prvaptmirror.source_sync.httpx.Client", fail_client)
    assert sync_source_once(ready, source.id)
    saved = get_source(conn, source.id)
    assert saved.last_status == "error"
    assert saved.consecutive_failures == 1
    run = conn.execute("SELECT * FROM source_runs WHERE source_id=?", (source.id,)).fetchone()
    assert run["status"] == "error"
    assert run["finished_at"]
    assert queue_source(conn, source.id)
    assert get_source(conn, source.id).last_status == "queued"
    conn.close()


def test_idle_scheduler_recovers_publish_and_pending_delete(ready, tmp_path, monkeypatch):
    from prvaptmirror import publish as publishing
    from prvaptmirror.db import get_setting
    from prvaptmirror.debparse import parse_deb
    from prvaptmirror.source_sync import run_due_sources

    incoming = build_deb(tmp_path / "delete-retry.deb", package="delete-retry")
    parsed = parse_deb(incoming, allowed_archs=ready.architectures)
    conn = connect(ready)
    results, published = publishing.upload_commit(ready, conn, [(parsed, incoming)], user_id=None)
    assert published.ok
    original_sign = publishing.sign_release

    def fail_sign(*args):
        raise OSError("test signing unavailable")

    monkeypatch.setattr(publishing, "sign_release", fail_sign)
    assert not publishing.delete_commit(ready, conn, results[0]["id"]).ok
    assert run_due_sources(ready) == 0
    assert get_setting(conn, "publish_dirty") == "1"
    monkeypatch.setattr(publishing, "sign_release", original_sign)
    assert run_due_sources(ready) == 0
    assert get_setting(conn, "publish_dirty") == "0"
    assert conn.execute("SELECT count(*) FROM packages").fetchone()[0] == 0
    assert not (ready.repo_dir / results[0]["filename"]).exists()
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


def test_manual_sync_retries_rejected_asset_with_compatible_deb(ready, tmp_path):
    repaired_deb = build_deb(
        tmp_path / "Bettbox.deb",
        package="Bettbox",
        version="1.19.0+2026090201",
        architecture="amd64",
        control_compress="zst",
        zstd_write_content_size=False,
    ).read_bytes()
    asset_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal asset_requests
        if request.url.path == "/repos/acme/tool/releases":
            return httpx.Response(
                200,
                json=[
                    {
                        "draft": False,
                        "prerelease": False,
                        "assets": [
                            {
                                "id": 201,
                                "name": "tool_1.19.0_amd64.deb",
                                "url": "https://api.github.com/repos/acme/tool/releases/assets/201",
                                "size": len(repaired_deb),
                                "updated_at": "2026-09-03T00:00:00Z",
                            }
                        ],
                    }
                ],
            )
        if request.url.path.endswith("/assets/201"):
            asset_requests += 1
            content = b"not a deb" if asset_requests == 1 else repaired_deb
            return httpx.Response(200, content=content)
        return httpx.Response(404)

    conn = connect(ready)
    source = create_source(ready, conn, _values(release_limit="1"))
    conn.close()
    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert sync_source_once(ready, source.id, client=client)
    conn = connect(ready)
    failed = get_source(conn, source.id)
    assert failed is not None and failed.last_status == "error"
    artifact = conn.execute(
        "SELECT * FROM source_artifacts WHERE source_id = ?", (source.id,)
    ).fetchone()
    assert artifact is not None and artifact["status"] == "rejected"
    assert queue_source(conn, source.id)
    conn.close()

    assert sync_source_once(ready, source.id, client=client)
    conn = connect(ready)
    repaired = get_source(conn, source.id)
    assert repaired is not None and repaired.last_status == "success"
    package = conn.execute("SELECT * FROM packages WHERE name = 'bettbox'").fetchone()
    assert package is not None
    artifact = conn.execute(
        "SELECT * FROM source_artifacts WHERE source_id = ?", (source.id,)
    ).fetchone()
    assert artifact is not None and artifact["status"] == "imported"
    assert asset_requests == 2
    conn.close()
    client.close()


def test_github_source_preview_uses_sync_selection_rules():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/tool/releases"
        assert request.url.params["per_page"] == "20"
        assert request.headers["authorization"] == "Bearer preview-token"
        return httpx.Response(
            200,
            json=[
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {"name": "tool_2.0.0_amd64.deb", "size": 1024},
                        {"name": "tool_2.0.0_arm64.deb", "size": 2048},
                        {"name": "tool_2.0.0_windows.zip", "size": 4096},
                    ],
                },
                {
                    "tag_name": "v2.1.0-rc1",
                    "draft": False,
                    "prerelease": True,
                    "assets": [{"name": "tool_rc_amd64.deb", "size": 512}],
                },
                {
                    "tag_name": "v3.0.0-draft",
                    "draft": True,
                    "prerelease": False,
                    "assets": [{"name": "tool_draft_amd64.deb", "size": 512}],
                },
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    preview = preview_github_source(
        "https://github.com/acme/tool/releases/latest",
        r"_amd64\.deb$",
        "2",
        token="preview-token",
        client=client,
    )
    assert preview.repository == "acme/tool"
    assert preview.releases_checked == 1
    assert preview.matched_count == 1
    assert [asset.filename for asset in preview.assets] == [
        "tool_2.0.0_amd64.deb",
        "tool_2.0.0_arm64.deb",
        "tool_2.0.0_windows.zip",
    ]
    assert [asset.selected for asset in preview.assets] == [True, False, False]
    assert preview.assets[1].is_deb and not preview.assets[1].pattern_matches
    assert not preview.assets[2].is_deb
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


def test_direct_url_reports_html_response_before_deb_parsing(ready):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>download error</html>",
            headers={"Content-Type": "text/html"},
        )

    conn = connect(ready)
    source = create_source(
        ready,
        conn,
        _values(
            name="HTML instead of deb",
            kind="direct_url",
            location="https://downloads.example.test/package.deb",
        ),
    )
    conn.close()
    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert sync_source_once(ready, source.id, client=client)
    conn = connect(ready)
    failed = get_source(conn, source.id)
    assert failed is not None and failed.last_status == "error"
    assert "下载响应不是 .deb" in (failed.last_error or "")
    assert "Content-Type text/html" in (failed.last_error or "")
    conn.close()
    client.close()


def test_http_directory_imports_only_latest_debian_version(ready, tmp_path):
    latest = build_deb(
        tmp_path / "tuxedo-yt6801_1.0.31-8_all.deb",
        package="tuxedo-yt6801",
        version="1.0.31-8",
        architecture="all",
    ).read_bytes()
    downloads: list[str] = []
    directory = "https://deb.example.test/pool/main/t/tuxedo-yt6801/"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == directory:
            return httpx.Response(
                200,
                text="""
                <html><body>
                  <a href="../">Parent</a>
                  <a href="tuxedo-yt6801_1.0.29tux0_all.deb">old</a>
                  <a href="tuxedo-yt6801_1.0.31-8_all.deb">latest</a>
                  <a href="https://other.example/evil_9.0_all.deb">external</a>
                  <a href="source_2.0.dsc">source</a>
                </body></html>
                """,
            )
        downloads.append(request.url.path)
        if request.url.path.endswith("tuxedo-yt6801_1.0.31-8_all.deb"):
            return httpx.Response(200, content=latest)
        return httpx.Response(404)

    conn = connect(ready)
    source = create_source(
        ready,
        conn,
        _values(
            name="TUXEDO YT6801",
            kind="direct_url",
            location=directory,
            asset_pattern=r"^tuxedo-yt6801_.*_all\.deb$",
        ),
    )
    conn.close()
    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert sync_source_once(ready, source.id, client=client)
    conn = connect(ready)
    package = conn.execute(
        "SELECT * FROM packages WHERE name = 'tuxedo-yt6801'"
    ).fetchone()
    assert package is not None and package["version"] == "1.0.31-8"
    run = conn.execute(
        "SELECT * FROM source_runs WHERE source_id = ? ORDER BY id DESC", (source.id,)
    ).fetchone()
    assert (run["status"], run["discovered"], run["downloaded"], run["imported"]) == (
        "success",
        1,
        1,
        1,
    )
    assert downloads == ["/pool/main/t/tuxedo-yt6801/tuxedo-yt6801_1.0.31-8_all.deb"]
    conn.close()
    client.close()


def test_http_directory_preview_probes_selected_deb_magic():
    directory = "https://deb.example.test/pool/tool/"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == directory:
            return httpx.Response(
                200,
                text="""
                <a href="tool_1.0_all.deb">old</a>
                <a href="tool_2.0_all.deb">latest</a>
                <a href="tool_2.0.dsc">source</a>
                """,
            )
        assert request.headers["range"] == "bytes=0-7"
        return httpx.Response(
            200,
            content=b"<html>not a deb</html>",
            headers={"Content-Type": "text/html"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    preview = preview_http_directory(
        directory,
        r"^tool_.*_all\.deb$",
        client=client,
    )

    assert preview.matched_count == 1
    assert preview.ready_count == 0
    selected = next(asset for asset in preview.assets if asset.selected)
    assert selected.filename == "tool_2.0_all.deb"
    assert selected.response_status == 200
    assert selected.content_type == "text/html"
    assert selected.valid_deb is False
    assert "不是 Debian ar 文件" in (selected.error or "")
    old = next(asset for asset in preview.assets if asset.filename == "tool_1.0_all.deb")
    assert not old.selected
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
