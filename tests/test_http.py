from __future__ import annotations

import re
from pathlib import Path

from tests.deb_builder import build_deb
from tests.conftest import ORIGIN

ORIGIN_HEADERS = {"Origin": ORIGIN}


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, html[:500]
    return match.group(1)


def login(client) -> None:
    page = client.get("/admin/login")
    assert page.status_code == 200
    token = _csrf(page.text)
    resp = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test-password-123", "csrf_token": token},
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code in {303, 302}, resp.text
    assert client.cookies.get("prvapt_session")


def test_login_without_origin_when_check_off(client):
    page = client.get("/admin/login")
    assert "个人apt仓库后台" in page.text
    assert "密码保存在本机" not in page.text
    assert 'class="login-form"' in page.text
    assert 'name="username" type="text"' in page.text
    assert 'name="password" type="password"' in page.text
    token = _csrf(page.text)
    resp = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test-password-123", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code in {302, 303}
    assert client.cookies.get("prvapt_session")


def test_unauthenticated_mutating_fails(client):
    resp = client.post(
        "/admin/packages/upload",
        data={"csrf_token": "x"},
        files={"files": ("x.deb", b"nope", "application/octet-stream")},
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code in {303, 401, 403}
    if resp.status_code in {303, 302}:
        assert "/admin/login" in resp.headers.get("location", "")


def test_health_ready_login_upload_inrelease(client, tmp_path: Path):
    health = client.get("/healthz")
    assert health.status_code == 200
    assert "ok" in health.text
    login(client)
    page = client.get("/admin/packages")
    assert "暂无软件包。" in page.text
    token = _csrf(page.text)
    deb = build_deb(
        tmp_path / "hello-prv_1.0-1_all.deb",
        package="hello-prv",
        version="1.0-1",
        architecture="all",
    )
    resp = client.post(
        "/admin/packages/upload",
        data={"csrf_token": token},
        files=[("files", (deb.name, deb.read_bytes(), "application/vnd.debian.binary-package"))],
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    assert resp.status_code in {303, 200, 409}, resp.text
    inrel = client.get("/apt/dists/stable/InRelease")
    assert inrel.status_code == 200
    assert "BEGIN PGP SIGNED MESSAGE" in inrel.text
    assert "Origin: PrvAptMirror" in inrel.text
    packages = client.get("/apt/dists/stable/main/binary-amd64/Packages")
    assert packages.status_code == 200
    assert "Package: hello-prv" in packages.text
    assert "Architecture: all" in packages.text
    match = re.search(r"Filename: (\S+)", packages.text)
    assert match
    blob = client.get("/apt/" + match.group(1))
    assert blob.status_code == 200
    assert blob.content == deb.read_bytes()
    # download via admin
    listed = client.get("/admin/packages")
    assert "hello-prv" in listed.text
    assert "有效" in listed.text
    detail_match = re.search(r'href="(/admin/packages/\d+)"', listed.text)
    assert detail_match
    detail = client.get(detail_match.group(1))
    assert "文件路径" in detail.text
    assert "软件包信息" in detail.text
    setup = client.get("/admin/setup")
    pres = re.findall(r"<pre>(.*?)</pre>", setup.text, flags=re.S)
    joined = "\n".join(pres).lower()
    assert "signed-by" in joined
    assert "trusted=yes" not in joined


def test_duplicate_http_rejected(client, tmp_path: Path):
    login(client)
    token = _csrf(client.get("/admin/packages").text)
    deb = build_deb(tmp_path / "dup_1.0-1_amd64.deb", package="duphttp", architecture="amd64")
    files = [("files", (deb.name, deb.read_bytes(), "application/octet-stream"))]
    first = client.post(
        "/admin/packages/upload",
        data={"csrf_token": token},
        files=files,
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    assert first.status_code in {200, 303}
    token = _csrf(client.get("/admin/packages").text)
    second = client.post(
        "/admin/packages/upload",
        data={"csrf_token": token},
        files=[("files", (deb.name, deb.read_bytes(), "application/octet-stream"))],
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    assert second.status_code in {409, 303}
    if second.status_code == 303:
        assert "duplicate" in second.headers.get("location", "")


def test_delete_http_drops_from_index(client, tmp_path: Path):
    login(client)
    token = _csrf(client.get("/admin/packages").text)
    deb = build_deb(tmp_path / "delme_1.0-1_all.deb", package="delme", architecture="all")
    client.post(
        "/admin/packages/upload",
        data={"csrf_token": token},
        files=[("files", (deb.name, deb.read_bytes(), "application/octet-stream"))],
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    listed = client.get("/admin/packages")
    match = re.search(r'href="/admin/packages/(\d+)"', listed.text)
    assert match
    pkg_id = match.group(1)
    token = _csrf(client.get(f"/admin/packages/{pkg_id}").text)
    deleted = client.post(
        f"/admin/packages/{pkg_id}/delete",
        data={"csrf_token": token, "confirm_name": "delme"},
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    assert deleted.status_code in {303, 200}
    packages = client.get("/apt/dists/stable/main/binary-all/Packages")
    assert "Package: delme\n" not in packages.text


def test_setup_snippet_no_trusted_yes(client):
    login(client)
    page = client.get("/admin/setup")
    assert "在 Debian/Ubuntu 客户端配置此 APT 源" in page.text
    assert "适用于 Debian 12+、Ubuntu 22.04+。" in page.text
    assert "用于传统 <code>sources.list</code> 单行格式。" in page.text
    pres = re.findall(r"<pre>(.*?)</pre>", page.text, flags=re.S)
    text = "\n".join(pres).lower()
    assert "signed-by" in text
    assert "trusted=yes" not in text
    assert "apt-key add" not in text
    assert "trusted.gpg.d" not in text


def test_source_configuration_ui_persists_in_database(client):
    class NoopScheduler:
        def wake(self):
            pass

    client.app.state.source_scheduler = NoopScheduler()
    login(client)
    page = client.get("/admin/sources")
    assert page.status_code == 200
    assert "软件来源" in page.text
    new_page = client.get("/admin/sources/new")
    response = client.post(
        "/admin/sources",
        data={
            "csrf_token": _csrf(new_page.text),
            "name": "GitHub example",
            "kind": "github_release",
            "location": "https://github.com/acme/example",
            "asset_pattern": r".*_amd64\.deb$",
            "interval_minutes": "45",
            "release_limit": "3",
            "include_prereleases": "yes",
            "token": "private-token",
        },
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    detail = client.get(response.headers["location"])
    assert "GitHub example" in detail.text
    assert "acme/example" in detail.text
    assert "private-token" not in detail.text
    assert "已配置；留空则保持不变" in detail.text
    assert (
        "GitHub 填写 <code>owner/repository</code>；HTTP 目录地址必须以 "
        "<code>/</code> 结尾。"
    ) in detail.text
    assert "保存后不再显示" in detail.text

    listing = client.get("/admin/sources")
    assert "GitHub example" in listing.text
    assert "已停用" in listing.text
    assert "后台调度器与网站运行在同一个容器内" not in listing.text


def test_source_form_can_preview_github_asset_matches(client, monkeypatch):
    from prvaptmirror.db import connect
    from prvaptmirror.routes import admin as admin_routes
    from prvaptmirror.source_sync import GitHubAssetPreview, GitHubSourcePreview

    login(client)
    page = client.get("/admin/sources/new")
    assert "测试获取与正则" in page.text
    assert 'hx-post="/admin/sources/test"' in page.text

    def fake_preview(location, asset_pattern, release_limit, **options):
        assert location == "https://github.com/acme/tool/releases/latest"
        assert asset_pattern == r"_amd64\.deb$"
        assert release_limit == "1"
        assert options["include_prereleases"] is False
        assert options["token"] == "temporary-token"
        return GitHubSourcePreview(
            repository="acme/tool",
            releases_checked=1,
            assets=(
                GitHubAssetPreview("v2.0.0", "tool_2.0.0_amd64.deb", 1024, True, True),
                GitHubAssetPreview("v2.0.0", "tool_2.0.0_arm64.deb", 2048, True, False),
                GitHubAssetPreview("v2.0.0", "checksums.txt", 128, False, False),
            ),
        )

    monkeypatch.setattr(admin_routes, "preview_github_source", fake_preview)
    response = client.post(
        "/admin/sources/test",
        data={
            "csrf_token": _csrf(page.text),
            "kind": "github_release",
            "location": "https://github.com/acme/tool/releases/latest",
            "asset_pattern": r"_amd64\.deb$",
            "release_limit": "1",
            "token": "temporary-token",
        },
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 200
    assert "1 个将被抓取" in response.text
    assert "tool_2.0.0_amd64.deb" in response.text
    assert "将抓取" in response.text
    assert "正则未命中" in response.text
    assert "不是 .deb" in response.text
    assert "temporary-token" not in response.text
    conn = connect(client.app.state.base_cfg)
    assert conn.execute("SELECT count(*) FROM package_sources").fetchone()[0] == 0
    conn.close()


def test_source_form_can_preview_http_directory_and_download_probe(client, monkeypatch):
    from prvaptmirror.routes import admin as admin_routes
    from prvaptmirror.source_sync import DirectoryAssetPreview, DirectorySourcePreview

    login(client)
    page = client.get("/admin/sources/new")
    directory = "https://deb.example.test/pool/tool/"

    def fake_preview(location, asset_pattern):
        assert location == directory
        assert asset_pattern == r"^tool_.*_all\.deb$"
        return DirectorySourcePreview(
            directory=directory,
            assets=(
                DirectoryAssetPreview(
                    filename="tool_1.0_all.deb",
                    version="1.0",
                    architecture="all",
                    is_deb=True,
                    pattern_matches=True,
                    valid_filename=True,
                    selected=False,
                ),
                DirectoryAssetPreview(
                    filename="tool_2.0_all.deb",
                    version="2.0",
                    architecture="all",
                    is_deb=True,
                    pattern_matches=True,
                    valid_filename=True,
                    selected=True,
                    response_status=200,
                    content_type="application/vnd.debian.binary-package",
                    valid_deb=True,
                ),
            ),
        )

    monkeypatch.setattr(admin_routes, "preview_http_directory", fake_preview)
    response = client.post(
        "/admin/sources/test",
        data={
            "csrf_token": _csrf(page.text),
            "kind": "direct_url",
            "location": directory,
            "asset_pattern": r"^tool_.*_all\.deb$",
            "release_limit": "1",
        },
        headers=ORIGIN_HEADERS,
    )

    assert response.status_code == 200
    assert "选中 1 个最新版" in response.text
    assert "1 个通过下载验证" in response.text
    assert "tool_2.0_all.deb" in response.text
    assert "最新版，可导入" in response.text
    assert "较旧版本" in response.text


def test_source_preview_shows_regex_validation_without_requesting_github(client):
    login(client)
    page = client.get("/admin/sources/new")
    response = client.post(
        "/admin/sources/test",
        data={
            "csrf_token": _csrf(page.text),
            "kind": "github_release",
            "location": "acme/tool",
            "asset_pattern": "[",
            "release_limit": "1",
        },
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 200
    assert "Asset 匹配表达式无效" in response.text


def test_source_manual_sync_is_queued_from_ui(client):
    from prvaptmirror.db import connect
    from prvaptmirror.sources import get_source

    class NoopScheduler:
        def wake(self):
            pass

    client.app.state.source_scheduler = NoopScheduler()
    login(client)
    new_page = client.get("/admin/sources/new")
    created = client.post(
        "/admin/sources",
        data={
            "csrf_token": _csrf(new_page.text),
            "name": "Queue example",
            "kind": "github_release",
            "location": "acme/queue-example",
            "asset_pattern": r".*\.deb$",
            "interval_minutes": "30",
            "release_limit": "1",
            "enabled": "yes",
        },
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    detail = client.get(created.headers["location"])
    source_id = int(re.search(r"/admin/sources/(\d+)", created.headers["location"]).group(1))
    queued = client.post(
        f"/admin/sources/{source_id}/sync",
        data={"csrf_token": _csrf(detail.text)},
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    assert queued.status_code == 303
    assert "queued=1" in queued.headers["location"]
    conn = connect(client.app.state.base_cfg)
    source = get_source(conn, source_id)
    assert source is not None and source.last_status == "queued"
    conn.close()


def test_settings_update_runtime_values_and_republish(client):
    login(client)
    page = client.get("/admin/settings")
    assert page.status_code == 200
    assert "公开 URL" in page.text
    assert "用于生成客户端接入命令。" in page.text
    assert "英文逗号分隔；移除后不再进入索引。" in page.text
    assert "会话期限只影响" not in page.text
    token = _csrf(page.text)
    values = {
        "csrf_token": token,
        "public_url": "https://apt.example.com",
        "suite": "stable",
        "codename": "stable",
        "component": "main",
        "architectures": "amd64,arm64,all",
        "origin": "PrvAptMirror",
        "label": "prvapt",
        "max_upload_mb": "256",
        "max_upload_files": "10",
        "session_days": "14",
    }
    saved = client.post(
        "/admin/settings",
        data=values,
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    assert "https://apt.example.com/apt" in client.get("/admin/setup").text

    page = client.get("/admin/settings")
    values.update(
        {
            "csrf_token": _csrf(page.text),
            "suite": "testing",
            "codename": "bookworm",
            "origin": "Private Repository",
            "confirm_repository_change": "yes",
        }
    )
    republished = client.post(
        "/admin/settings",
        data=values,
        headers=ORIGIN_HEADERS,
        follow_redirects=False,
    )
    assert republished.status_code == 303, republished.text
    inrelease = client.get("/apt/dists/testing/InRelease")
    assert inrelease.status_code == 200
    assert "Suite: testing" in inrelease.text
    assert "Codename: bookworm" in inrelease.text
    assert "Origin: Private Repository" in inrelease.text


def test_settings_reject_repository_change_without_confirmation(client):
    login(client)
    page = client.get("/admin/settings")
    response = client.post(
        "/admin/settings",
        data={
            "csrf_token": _csrf(page.text),
            "public_url": ORIGIN,
            "suite": "testing",
            "codename": "testing",
            "component": "main",
            "architectures": "amd64,arm64,all",
            "origin": "PrvAptMirror",
            "label": "prvapt",
            "max_upload_mb": "512",
            "max_upload_files": "20",
            "session_days": "7",
        },
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 400
    assert "请勾选确认" in response.text


def test_settings_roll_back_when_republish_fails(client, monkeypatch):
    from prvaptmirror.models import PublishResult
    from prvaptmirror.routes import admin as admin_routes

    login(client)
    calls = 0

    def fail_then_recover(cfg, conn):
        nonlocal calls
        calls += 1
        if calls == 1:
            return PublishResult(ok=False, error="simulated signing failure")
        return PublishResult(ok=True)

    monkeypatch.setattr(admin_routes, "publish_unlocked", fail_then_recover)
    page = client.get("/admin/settings")
    response = client.post(
        "/admin/settings",
        data={
            "csrf_token": _csrf(page.text),
            "public_url": ORIGIN,
            "suite": "broken-release",
            "codename": "stable",
            "component": "main",
            "architectures": "amd64,arm64,all",
            "origin": "PrvAptMirror",
            "label": "prvapt",
            "max_upload_mb": "512",
            "max_upload_files": "20",
            "session_days": "7",
            "confirm_repository_change": "yes",
        },
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 500
    assert "设置已回滚" in response.text
    assert calls == 2
    settings = client.get("/admin/settings")
    assert 'name="suite" type="text" required value="stable"' in settings.text


def test_startup_recovers_interrupted_settings_publish(ready):
    from fastapi.testclient import TestClient

    from prvaptmirror.db import connect, get_setting, set_setting
    from prvaptmirror.main import create_app
    from prvaptmirror.settings import SETTINGS_PENDING_KEY, ensure_app_settings

    conn = connect(ready)
    ensure_app_settings(conn, ready)
    set_setting(conn, SETTINGS_PENDING_KEY, "1")
    conn.close()

    app = create_app(ready)
    with TestClient(app, base_url=ORIGIN) as recovered:
        assert recovered.get("/readyz").status_code == 200

    conn = connect(ready)
    assert get_setting(conn, SETTINGS_PENDING_KEY) == "0"
    conn.close()
