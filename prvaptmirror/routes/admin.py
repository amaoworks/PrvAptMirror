"""Password-protected admin UI. Mutating routes require a session."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from prvaptmirror.auth import (
    SESSION_COOKIE,
    authenticate,
    change_password,
    create_session,
    delete_session,
    load_session_user,
)
from prvaptmirror.config import Config
from prvaptmirror.csrf import (
    COOKIE as CSRF_COOKIE,
    FORM_FIELD,
    issue_anon_token,
    session_token,
    set_csrf_cookie,
    verify_csrf,
)
from prvaptmirror.db import (
    connect,
    get_package,
    get_setting,
    last_publish_row,
    list_packages,
)
from prvaptmirror.debparse import DebParseError, parse_deb
from prvaptmirror.events import emit
from prvaptmirror.models import User
from prvaptmirror.publish import delete_commit, publish, upload_commit
from prvaptmirror.ratelimit import client_ip, cookie_secure_flag, is_locked, record_attempt
from prvaptmirror.snippets import deb822_snippet, oneline_snippet
from prvaptmirror.storage import DiskFullError, disk_preflight, write_incoming_stream

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def humansize(num: object) -> str:
    """Binary units starting at MB (never B/KB)."""
    try:
        n = int(num or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 0:
        n = 0
    mb = 1024 * 1024
    gb = mb * 1024
    tb = gb * 1024
    if n >= tb:
        return f"{n / tb:.2f} TB"
    if n >= gb:
        return f"{n / gb:.2f} GB"
    return f"{n / mb:.2f} MB"


templates.env.filters["humansize"] = humansize

router = APIRouter()


def _cfg(request: Request) -> Config:
    return request.app.state.cfg


def _csrf_expected(request: Request, user: User | None) -> str:
    cfg = _cfg(request)
    if user is not None:
        token = request.cookies.get(SESSION_COOKIE, "")
        from prvaptmirror.auth import hash_session_token

        return session_token(cfg.secret_key, hash_session_token(token))
    existing = request.cookies.get(CSRF_COOKIE)
    return existing or ""


def _attach_csrf(request: Request, response: Response, user: User | None) -> str:
    cfg = _cfg(request)
    secure = cookie_secure_flag(request, cfg)
    if user is not None:
        token = _csrf_expected(request, user)
        set_csrf_cookie(response, token, secure=secure, max_age=cfg.session_days * 86400)
        return token
    token = request.cookies.get(CSRF_COOKIE) or issue_anon_token()
    set_csrf_cookie(response, token, secure=secure, max_age=86400)
    return token


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    cfg = _cfg(request)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=cfg.session_days * 86400,
        httponly=True,
        samesite="lax",
        path="/admin",
        secure=cookie_secure_flag(request, cfg),
    )


def current_user(request: Request) -> User | None:
    cfg = _cfg(request)
    conn = connect(cfg)
    try:
        if cfg.insecure_no_auth:
            row = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
            if row:
                from prvaptmirror.db import user_from_row

                return user_from_row(row)
        return load_session_user(conn, request.cookies.get(SESSION_COOKIE))
    finally:
        conn.close()


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise _login_redirect(request)
    return user


class LoginRedirect(Exception):
    def __init__(self, location: str) -> None:
        self.location = location


def _login_redirect(request: Request) -> LoginRedirect:
    return LoginRedirect("/admin/login")


def _flash_ctx(
    request: Request,
    user: User | None,
    csrf: str,
    extra: dict | None = None,
) -> dict:
    ctx = {"request": request, "user": user, "csrf_token": csrf, "form_field": FORM_FIELD}
    if extra:
        ctx.update(extra)
    return ctx


@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if current_user(request):
        return RedirectResponse("/admin/", status_code=303)
    token = request.cookies.get(CSRF_COOKIE) or issue_anon_token()
    response = templates.TemplateResponse(
        request, "login.html", _flash_ctx(request, None, token)
    )
    _attach_csrf(request, response, None)
    set_csrf_cookie(
        response,
        token,
        secure=cookie_secure_flag(request, _cfg(request)),
        max_age=86400,
    )
    return response


@router.post("/login")
async def login_post(
    request: Request,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    cfg = _cfg(request)
    expected = request.cookies.get(CSRF_COOKIE, "")
    if not verify_csrf(request, cfg, csrf_token, expected):
        response = templates.TemplateResponse(
            request,
            "login.html",
            _flash_ctx(request, None, expected, {"error": "CSRF 校验失败，请刷新后重试"}),
            status_code=400,
        )
        _attach_csrf(request, response, None)
        return response
    conn = connect(cfg)
    try:
        ip = client_ip(request, cfg)
        if is_locked(conn, ip):
            emit("login_fail", reason="rate_limit", ip=ip)
            response = templates.TemplateResponse(
                request,
                "login.html",
                _flash_ctx(request, None, expected, {"error": "登录失败次数过多，请 15 分钟后再试"}),
                status_code=429,
            )
            _attach_csrf(request, response, None)
            return response
        user = authenticate(conn, username.strip(), password)
        if user is None:
            record_attempt(conn, ip, username.strip(), False)
            emit("login_fail", reason="bad_credentials", ip=ip)
            response = templates.TemplateResponse(
                request,
                "login.html",
                _flash_ctx(request, None, expected, {"error": "用户名或密码错误"}),
                status_code=401,
            )
            _attach_csrf(request, response, None)
            return response
        record_attempt(conn, ip, username.strip(), True)
        token = create_session(
            conn,
            user,
            days=cfg.session_days,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
        emit("login_ok", user=user.username, ip=ip)
        dest = "/admin/password" if user.must_change_password else "/admin/"
        response = RedirectResponse(dest, status_code=303)
        _set_session_cookie(request, response, token)
        _attach_csrf(request, response, user)
        # session csrf uses the new cookie; set expected from new token
        from prvaptmirror.auth import hash_session_token
        from prvaptmirror.csrf import session_token as st

        set_csrf_cookie(
            response,
            st(cfg.secret_key, hash_session_token(token)),
            secure=cookie_secure_flag(request, cfg),
            max_age=cfg.session_days * 86400,
        )
        return response
    finally:
        conn.close()


@router.post("/logout")
async def logout(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
):
    cfg = _cfg(request)
    user = current_user(request)
    expected = _csrf_expected(request, user)
    if not verify_csrf(request, cfg, csrf_token, expected):
        return RedirectResponse("/admin/login", status_code=303)
    conn = connect(cfg)
    try:
        delete_session(conn, request.cookies.get(SESSION_COOKIE))
    finally:
        conn.close()
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return response


def _need_user(request: Request) -> User | RedirectResponse:
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    return user


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
    cfg = _cfg(request)
    conn = connect(cfg)
    try:
        packages = list_packages(conn)
        last = last_publish_row(conn)
        fpr = get_setting(conn, "gpg_fingerprint") or ""
        dirty = get_setting(conn, "publish_dirty", "0")
    finally:
        conn.close()
    skipped = [p for p in packages if p.architecture not in cfg.architectures]
    counts = {
        "total": len(packages),
        "active": sum(1 for p in packages if p.state == "active"),
        "pending_delete": sum(1 for p in packages if p.state == "pending_delete"),
        "missing": sum(1 for p in packages if p.state == "missing"),
        "bytes": sum(p.size for p in packages if p.state == "active"),
    }
    disk = os.statvfs(cfg.data_dir)
    free = disk.f_bavail * disk.f_frsize
    response = templates.TemplateResponse(
        request,
        "dashboard.html",
        _flash_ctx(
            request,
            user,
            _csrf_expected(request, user),
            {
                "packages": packages,
                "counts": counts,
                "skipped": skipped,
                "last": last,
                "fingerprint": fpr,
                "dirty": dirty,
                "disk_free": free,
                "cfg": cfg,
            },
        ),
    )
    _attach_csrf(request, response, user)
    return response


@router.get("/setup", response_class=HTMLResponse)
def setup(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    response = templates.TemplateResponse(
        request,
        "setup.html",
        _flash_ctx(
            request,
            user,
            _csrf_expected(request, user),
            {
                "deb822": deb822_snippet(cfg),
                "oneline": oneline_snippet(cfg),
                "cfg": cfg,
            },
        ),
    )
    _attach_csrf(request, response, user)
    return response


@router.get("/packages", response_class=HTMLResponse)
def packages_list(request: Request, q: str = "", arch: str = ""):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    conn = connect(cfg)
    try:
        rows = list_packages(conn, q=q or None, arch=arch or None)
    finally:
        conn.close()
    response = templates.TemplateResponse(
        request,
        "packages.html",
        _flash_ctx(
            request,
            user,
            _csrf_expected(request, user),
            {"packages": rows, "q": q, "arch": arch, "cfg": cfg},
        ),
    )
    _attach_csrf(request, response, user)
    return response


@router.get("/packages/{package_id}", response_class=HTMLResponse)
def package_detail(request: Request, package_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    conn = connect(cfg)
    try:
        row = get_package(conn, package_id)
    finally:
        conn.close()
    if row is None:
        return HTMLResponse("not found", status_code=404)
    control = json.loads(row.control_json)
    response = templates.TemplateResponse(
        request,
        "package_detail.html",
        _flash_ctx(
            request,
            user,
            _csrf_expected(request, user),
            {"pkg": row, "control": control},
        ),
    )
    _attach_csrf(request, response, user)
    return response


@router.post("/packages/upload")
async def upload_packages(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    files: list[UploadFile] = File(default=[]),
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    expected = _csrf_expected(request, user)
    if not verify_csrf(request, cfg, csrf_token, expected):
        return HTMLResponse("CSRF 校验失败", status_code=403)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
    uploads = [f for f in files if f.filename]
    if not uploads:
        return RedirectResponse("/admin/packages?err=nofile", status_code=303)
    if len(uploads) > cfg.max_upload_files:
        return RedirectResponse("/admin/packages?err=too_many", status_code=303)
    incoming_paths: list[Path] = []
    items = []
    try:
        total = 0
        for up in uploads:
            name = up.filename or "upload.deb"
            if not name.lower().endswith(".deb"):
                emit("upload_reject", reason="extension", filename=name)
                return RedirectResponse("/admin/packages?err=notdeb", status_code=303)
            chunks = []
            size = 0
            while True:
                chunk = await up.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                total += len(chunk)
                if size > cfg.max_upload_bytes or total > cfg.max_upload_bytes:
                    return HTMLResponse("上传超过大小限制", status_code=413)
                chunks.append(chunk)
            data = b"".join(chunks)

            def stream():
                yield data

            path = write_incoming_stream(cfg, stream(), limit=cfg.max_upload_bytes)
            incoming_paths.append(path)
            try:
                parsed = parse_deb(path, allowed_archs=cfg.architectures)
            except DebParseError as exc:
                path.unlink(missing_ok=True)
                emit("upload_reject", reason=str(exc), filename=name)
                return RedirectResponse(
                    "/admin/packages?err=" + quote(str(exc), safe=""),
                    status_code=303,
                )
            items.append((parsed, path))
        disk_preflight(cfg.repo_dir, total)

        def work():
            conn = connect(cfg)
            try:
                return upload_commit(cfg, conn, items, user_id=user.id)
            finally:
                conn.close()

        loop = asyncio.get_running_loop()
        results, pub = await loop.run_in_executor(None, work)
        if any(not r["ok"] and r.get("error") == "duplicate" for r in results) and not any(
            r["ok"] for r in results
        ):
            return RedirectResponse("/admin/packages?err=duplicate", status_code=409)
        return RedirectResponse("/admin/packages?ok=1", status_code=303)
    except DiskFullError:
        for path in incoming_paths:
            path.unlink(missing_ok=True)
        return HTMLResponse("磁盘空间不足", status_code=507)
    except Exception:
        for path in incoming_paths:
            path.unlink(missing_ok=True)
        raise


@router.get("/packages/{package_id}/download")
def download_package(request: Request, package_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    conn = connect(cfg)
    try:
        row = get_package(conn, package_id)
    finally:
        conn.close()
    if row is None:
        return HTMLResponse("not found", status_code=404)
    blob = cfg.repo_dir / row.filename
    if not blob.is_file():
        return HTMLResponse("blob missing", status_code=404)
    filename = blob.name
    return FileResponse(
        blob,
        media_type="application/vnd.debian.binary-package",
        filename=filename,
    )


@router.post("/packages/{package_id}/delete")
async def delete_package(
    request: Request,
    package_id: int,
    csrf_token: Annotated[str, Form()] = "",
    confirm_name: Annotated[str, Form()] = "",
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    expected = _csrf_expected(request, user)
    if not verify_csrf(request, cfg, csrf_token, expected):
        return HTMLResponse("CSRF 校验失败", status_code=403)
    conn = connect(cfg)
    try:
        row = get_package(conn, package_id)
        if row is None:
            return HTMLResponse("not found", status_code=404)
        if confirm_name.strip() != row.name:
            return RedirectResponse(f"/admin/packages/{package_id}?err=confirm", status_code=303)

        def work():
            inner = connect(cfg)
            try:
                return delete_commit(cfg, inner, package_id)
            finally:
                inner.close()

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, work)
        if not result.ok:
            return RedirectResponse("/admin/packages?err=publish", status_code=303)
        return RedirectResponse("/admin/packages?deleted=1", status_code=303)
    finally:
        conn.close()


@router.get("/password", response_class=HTMLResponse)
def password_get(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    response = templates.TemplateResponse(
        request,
        "password.html",
        _flash_ctx(request, user, _csrf_expected(request, user), {"forced": user.must_change_password}),
    )
    _attach_csrf(request, response, user)
    return response


@router.post("/password")
async def password_post(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    new_password2: Annotated[str, Form()] = "",
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    expected = _csrf_expected(request, user)
    if not verify_csrf(request, cfg, csrf_token, expected):
        return HTMLResponse("CSRF 校验失败", status_code=403)
    if len(new_password) < 10 or new_password != new_password2:
        response = templates.TemplateResponse(
            request,
            "password.html",
            _flash_ctx(
                request,
                user,
                expected,
                {"forced": user.must_change_password, "error": "两次输入须一致且至少 10 个字符"},
            ),
            status_code=400,
        )
        _attach_csrf(request, response, user)
        return response
    conn = connect(cfg)
    try:
        change_password(conn, user, new_password)
        bootstrap = cfg.bootstrap_path
        bootstrap.unlink(missing_ok=True)
        token = create_session(
            conn,
            user,
            days=cfg.session_days,
            ip=client_ip(request, cfg),
            user_agent=request.headers.get("user-agent"),
        )
    finally:
        conn.close()
    response = RedirectResponse("/admin/", status_code=303)
    _set_session_cookie(request, response, token)
    from prvaptmirror.auth import hash_session_token
    from prvaptmirror.csrf import session_token as st
    from prvaptmirror.csrf import set_csrf_cookie

    set_csrf_cookie(
        response,
        st(cfg.secret_key, hash_session_token(token)),
        secure=cookie_secure_flag(request, cfg),
        max_age=cfg.session_days * 86400,
    )
    return response


@router.post("/publish")
async def publish_now(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    expected = _csrf_expected(request, user)
    if not verify_csrf(request, cfg, csrf_token, expected):
        return HTMLResponse("CSRF 校验失败", status_code=403)

    def work():
        conn = connect(cfg)
        try:
            return publish(cfg, conn)
        finally:
            conn.close()

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, work)
    return RedirectResponse("/admin/", status_code=303)
