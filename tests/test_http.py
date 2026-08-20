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
    pres = re.findall(r"<pre>(.*?)</pre>", page.text, flags=re.S)
    text = "\n".join(pres).lower()
    assert "signed-by" in text
    assert "trusted=yes" not in text
    assert "apt-key add" not in text
    assert "trusted.gpg.d" not in text
