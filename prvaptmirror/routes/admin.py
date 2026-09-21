"""Password-protected admin UI. Mutating routes require a session."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
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
    origin_allowed,
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
    set_setting,
    transaction,
)
from prvaptmirror.debparse import DebParseError, parse_deb
from prvaptmirror.events import emit
from prvaptmirror.models import User
from prvaptmirror.publish import (
    delete_commit,
    publish,
    publish_lock,
    publish_unlocked,
    upload_commit,
)
from prvaptmirror.ratelimit import client_ip, cookie_secure_flag, is_locked, record_attempt
from prvaptmirror.snippets import deb822_snippet, oneline_snippet
from prvaptmirror.storage import DiskFullError, disk_preflight, write_incoming_stream
from prvaptmirror.uploads import bounded_upload_form
from prvaptmirror.sources import (
    DEFAULT_ASSET_PATTERN,
    SourceValidationError,
    create_source,
    decrypt_token,
    delete_source,
    get_source,
    list_sources,
    queue_source,
    source_artifacts,
    source_runs,
    update_source,
)
from prvaptmirror.settings import (
    SettingsValidationError,
    SETTINGS_PENDING_KEY,
    app_setting_values,
    config_from_app_values,
    load_app_config,
    repository_settings_changed,
    save_app_config,
)
from prvaptmirror.source_sync import preview_github_source, preview_http_directory

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

STATUS_LABELS = {
    "active": "有效",
    "pending_delete": "删除中",
    "missing": "缺失",
    "success": "成功",
    "failed": "失败",
    "error": "失败",
    "running": "进行中",
    "queued": "等待同步",
    "never": "尚未检查",
    "pending": "等待处理",
    "imported": "已导入",
    "skipped": "已跳过",
    "rejected": "已拒绝",
    "conflict": "冲突",
}


def status_label(value: object) -> str:
    text = str(value or "")
    return STATUS_LABELS.get(text, text)


templates.env.filters["status_label"] = status_label

router = APIRouter()


def _cfg(request: Request) -> Config:
    return request.state.cfg


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


def _settings_response(
    request: Request,
    user: User,
    values: dict[str, str],
    *,
    error: str | None = None,
    status_code: int = 200,
):
    response = templates.TemplateResponse(
        request,
        "settings.html",
        _flash_ctx(
            request,
            user,
            _csrf_expected(request, user),
            {
                "values": values,
                "error": error,
                "saved": request.query_params.get("saved") == "1",
            },
        ),
        status_code=status_code,
    )
    _attach_csrf(request, response, user)
    return response


def _source_values(source=None) -> dict[str, str]:
    if source is None:
        return {
            "name": "",
            "kind": "github_release",
            "location": "",
            "asset_pattern": DEFAULT_ASSET_PATTERN,
            "interval_minutes": "30",
            "release_limit": "1",
            "include_prereleases": "",
            "enabled": "yes",
        }
    return {
        "name": source.name,
        "kind": source.kind,
        "location": source.location,
        "asset_pattern": source.asset_pattern,
        "interval_minutes": str(source.interval_minutes),
        "release_limit": str(source.release_limit),
        "include_prereleases": "yes" if source.include_prereleases else "",
        "enabled": "yes" if source.enabled else "",
    }


def _source_form_response(
    request: Request,
    user: User,
    values: dict[str, str],
    *,
    source=None,
    error: str | None = None,
    status_code: int = 200,
):
    cfg = _cfg(request)
    conn = connect(cfg)
    try:
        runs = source_runs(conn, source.id) if source else []
        artifacts = source_artifacts(conn, source.id) if source else []
    finally:
        conn.close()
    response = templates.TemplateResponse(
        request,
        "source_form.html",
        _flash_ctx(
            request,
            user,
            _csrf_expected(request, user),
            {
                "source": source,
                "values": values,
                "token_configured": bool(source and source.token_encrypted),
                "runs": runs,
                "artifacts": artifacts,
                "error": error,
            },
        ),
        status_code=status_code,
    )
    _attach_csrf(request, response, user)
    return response


@router.get("/sources", response_class=HTMLResponse)
def sources_list(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
    cfg = _cfg(request)
    conn = connect(cfg)
    try:
        rows = list_sources(conn)
    finally:
        conn.close()
    response = templates.TemplateResponse(
        request,
        "sources.html",
        _flash_ctx(
            request,
            user,
            _csrf_expected(request, user),
            {"sources": rows},
        ),
    )
    _attach_csrf(request, response, user)
    return response


@router.get("/sources/new", response_class=HTMLResponse)
def source_new(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
    return _source_form_response(request, user, _source_values())


@router.post("/sources")
async def source_create(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    asset_pattern: Annotated[str, Form()] = "",
    interval_minutes: Annotated[str, Form()] = "30",
    release_limit: Annotated[str, Form()] = "1",
    include_prereleases: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
    token: Annotated[str, Form()] = "",
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    if not verify_csrf(request, cfg, csrf_token, _csrf_expected(request, user)):
        return HTMLResponse("CSRF 校验失败", status_code=403)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
    values = {
        "name": name,
        "kind": kind,
        "location": location,
        "asset_pattern": asset_pattern,
        "interval_minutes": interval_minutes,
        "release_limit": release_limit,
        "include_prereleases": include_prereleases,
        "enabled": enabled,
    }
    conn = connect(cfg)
    try:
        try:
            source = create_source(cfg, conn, values, token)
        except SourceValidationError as exc:
            return _source_form_response(
                request, user, values, error=str(exc), status_code=400
            )
    finally:
        conn.close()
    request.app.state.source_scheduler.wake()
    return RedirectResponse(f"/admin/sources/{source.id}?created=1", status_code=303)


def _source_test_error(request: Request, message: str):
    return templates.TemplateResponse(
        request,
        "source_test_result.html",
        {"error": message, "preview": None},
    )


@router.post("/sources/test", response_class=HTMLResponse)
async def source_test(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    source_id: Annotated[int, Form()] = 0,
    kind: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    asset_pattern: Annotated[str, Form()] = "",
    release_limit: Annotated[str, Form()] = "1",
    include_prereleases: Annotated[str, Form()] = "",
    token: Annotated[str, Form()] = "",
    clear_token: Annotated[str, Form()] = "",
):
    user = current_user(request)
    if user is None:
        return HTMLResponse("请先登录", status_code=401)
    cfg = _cfg(request)
    if not verify_csrf(request, cfg, csrf_token, _csrf_expected(request, user)):
        return HTMLResponse("CSRF 校验失败", status_code=403)
    if user.must_change_password:
        return HTMLResponse("请先修改初始密码", status_code=403)
    is_github = kind == "github_release"
    try:
        if is_github:
            test_token = token.strip()
            if not test_token and source_id > 0 and clear_token != "yes":
                conn = connect(cfg)
                try:
                    saved_source = get_source(conn, source_id)
                    if saved_source is not None:
                        test_token = decrypt_token(cfg, saved_source.token_encrypted) or ""
                finally:
                    conn.close()
            preview = await asyncio.to_thread(
                preview_github_source,
                location,
                asset_pattern,
                release_limit,
                include_prereleases=include_prereleases == "yes",
                token=test_token,
            )
        elif kind == "direct_url" and location.strip().endswith("/"):
            preview = await asyncio.to_thread(
                preview_http_directory,
                location,
                asset_pattern,
            )
        elif kind == "direct_url":
            return _source_test_error(request, "单个固定 .deb 地址无需目录匹配测试。")
        else:
            return _source_test_error(request, "不支持的软件来源类型。")
    except SourceValidationError as exc:
        return _source_test_error(request, str(exc))
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if not is_github:
            message = f"HTTP 目录返回 {status}，请检查地址和访问权限。"
        elif status == 401:
            message = "GitHub 拒绝了凭据，请检查 Token。"
        elif status == 403:
            message = "GitHub 拒绝了请求，可能已达到 API 限额或 Token 权限不足。"
        elif status == 404:
            message = "找不到 GitHub 仓库；请检查地址，私有仓库还需要有效 Token。"
        else:
            message = f"GitHub API 返回 HTTP {status}。"
        return _source_test_error(request, message)
    except httpx.TimeoutException:
        return _source_test_error(request, "连接远端超时，请稍后重试。")
    except httpx.RequestError as exc:
        return _source_test_error(request, f"连接远端失败：{str(exc)[:300]}")
    except (RuntimeError, ValueError) as exc:
        return _source_test_error(request, str(exc)[:500])
    return templates.TemplateResponse(
        request,
        "source_test_result.html",
        {"error": None, "preview": preview},
    )


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def source_edit(request: Request, source_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
    cfg = _cfg(request)
    conn = connect(cfg)
    try:
        source = get_source(conn, source_id)
    finally:
        conn.close()
    if source is None:
        return HTMLResponse("not found", status_code=404)
    return _source_form_response(request, user, _source_values(source), source=source)


@router.post("/sources/{source_id}")
async def source_update(
    request: Request,
    source_id: int,
    csrf_token: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    asset_pattern: Annotated[str, Form()] = "",
    interval_minutes: Annotated[str, Form()] = "30",
    release_limit: Annotated[str, Form()] = "1",
    include_prereleases: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
    token: Annotated[str, Form()] = "",
    clear_token: Annotated[str, Form()] = "",
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    if not verify_csrf(request, cfg, csrf_token, _csrf_expected(request, user)):
        return HTMLResponse("CSRF 校验失败", status_code=403)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
    values = {
        "name": name,
        "kind": kind,
        "location": location,
        "asset_pattern": asset_pattern,
        "interval_minutes": interval_minutes,
        "release_limit": release_limit,
        "include_prereleases": include_prereleases,
        "enabled": enabled,
    }
    conn = connect(cfg)
    try:
        existing = get_source(conn, source_id)
        if existing is None:
            return HTMLResponse("not found", status_code=404)
        try:
            source = update_source(
                cfg,
                conn,
                source_id,
                values,
                token=token,
                clear_token=clear_token == "yes",
            )
        except SourceValidationError as exc:
            return _source_form_response(
                request,
                user,
                values,
                source=existing,
                error=str(exc),
                status_code=400,
            )
    finally:
        conn.close()
    request.app.state.source_scheduler.wake()
    return RedirectResponse(f"/admin/sources/{source.id}?saved=1", status_code=303)


@router.post("/sources/{source_id}/sync")
async def source_sync_now(
    request: Request,
    source_id: int,
    csrf_token: Annotated[str, Form()] = "",
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    if not verify_csrf(request, cfg, csrf_token, _csrf_expected(request, user)):
        return HTMLResponse("CSRF 校验失败", status_code=403)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
    conn = connect(cfg)
    try:
        queued = queue_source(conn, source_id)
    finally:
        conn.close()
    if not queued:
        return RedirectResponse(f"/admin/sources/{source_id}?err=disabled", status_code=303)
    request.app.state.source_scheduler.wake()
    return RedirectResponse(f"/admin/sources/{source_id}?queued=1", status_code=303)


@router.post("/sources/{source_id}/delete")
async def source_delete(
    request: Request,
    source_id: int,
    csrf_token: Annotated[str, Form()] = "",
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    cfg = _cfg(request)
    if not verify_csrf(request, cfg, csrf_token, _csrf_expected(request, user)):
        return HTMLResponse("CSRF 校验失败", status_code=403)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
    conn = connect(cfg)
    try:
        source = get_source(conn, source_id)
        if source is None:
            return HTMLResponse("not found", status_code=404)
        if source.last_status == "running":
            return RedirectResponse(f"/admin/sources/{source_id}?err=running", status_code=303)
        delete_source(conn, source_id)
    finally:
        conn.close()
    return RedirectResponse("/admin/sources?deleted=1", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
    return _settings_response(request, user, app_setting_values(_cfg(request)))


class _SettingsPublishFailure(RuntimeError):
    pass


@router.post("/settings")
async def settings_post(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    public_url: Annotated[str, Form()] = "",
    suite: Annotated[str, Form()] = "",
    codename: Annotated[str, Form()] = "",
    component: Annotated[str, Form()] = "",
    architectures: Annotated[str, Form()] = "",
    origin: Annotated[str, Form()] = "",
    label: Annotated[str, Form()] = "",
    max_upload_mb: Annotated[str, Form()] = "",
    max_upload_files: Annotated[str, Form()] = "",
    session_days: Annotated[str, Form()] = "",
    confirm_repository_change: Annotated[str, Form()] = "",
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    current = _cfg(request)
    expected = _csrf_expected(request, user)
    if not verify_csrf(request, current, csrf_token, expected):
        return HTMLResponse("CSRF 校验失败", status_code=403)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)

    values = {
        "public_url": public_url,
        "suite": suite,
        "codename": codename,
        "component": component,
        "architectures": architectures,
        "origin": origin,
        "label": label,
        "max_upload_mb": max_upload_mb,
        "max_upload_files": max_upload_files,
        "session_days": session_days,
    }
    try:
        candidate = config_from_app_values(request.app.state.base_cfg, values)
    except (SettingsValidationError, RuntimeError) as exc:
        return _settings_response(request, user, values, error=str(exc), status_code=400)

    if repository_settings_changed(current, candidate) and confirm_repository_change != "yes":
        return _settings_response(
            request,
            user,
            values,
            error="仓库元数据发生变化，请勾选确认后再保存",
            status_code=400,
        )

    def work() -> str | None:
        base = request.app.state.base_cfg
        conn = connect(base)
        try:
            before = load_app_config(base, conn)
            if not repository_settings_changed(before, candidate):
                with transaction(conn):
                    save_app_config(conn, candidate)
                return None

            with publish_lock(before):
                set_setting(conn, SETTINGS_PENDING_KEY, "1")
                try:
                    result = publish_unlocked(candidate, conn)
                    if not result.ok:
                        raise _SettingsPublishFailure(result.error or "索引重建失败")
                    with transaction(conn):
                        save_app_config(conn, candidate)
                        set_setting(conn, SETTINGS_PENDING_KEY, "0")
                except Exception as exc:
                    try:
                        recovery = publish_unlocked(before, conn)
                    except Exception as recovery_exc:
                        return f"{exc}；旧配置索引恢复也失败：{recovery_exc}"
                    if not recovery.ok:
                        return f"{exc}；旧配置索引恢复也失败：{recovery.error}"
                    set_setting(conn, SETTINGS_PENDING_KEY, "0")
                    return f"{exc}；设置已回滚"
            return None
        finally:
            conn.close()

    loop = asyncio.get_running_loop()
    error = await loop.run_in_executor(None, work)
    if error:
        return _settings_response(request, user, values, error=error, status_code=500)
    request.app.state.cfg = candidate
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


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
async def upload_packages(request: Request):
    # No Form/File parameters: authentication must precede multipart parsing.
    user = await run_in_threadpool(current_user, request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
    cfg = _cfg(request)
    if not origin_allowed(request, cfg):
        return HTMLResponse("CSRF 校验失败", status_code=403)
    incoming_paths: list[Path] = []
    items = []
    try:
        async with bounded_upload_form(request, limit=cfg.max_upload_bytes, max_files=cfg.max_upload_files) as form:
            token = form.get("csrf_token", "")
            if not isinstance(token, str) or not verify_csrf(request, cfg, token, _csrf_expected(request, user)):
                return HTMLResponse("CSRF 校验失败", status_code=403)
            uploads = [up for up in form.getlist("files") if isinstance(up, UploadFile) and up.filename]
            if not uploads:
                return RedirectResponse("/admin/packages?err=nofile", status_code=303)
            total = 0
            for up in uploads:
                name = up.filename
                if not name.lower().endswith(".deb"):
                    emit("upload_reject", reason="extension", filename=name)
                    return RedirectResponse("/admin/packages?err=notdeb", status_code=303)

                def prepare_file():
                    chunks = iter(lambda: up.file.read(1024 * 1024), b"")
                    path = write_incoming_stream(cfg, chunks, limit=cfg.max_upload_bytes - total)
                    try:
                        parsed = parse_deb(path, allowed_archs=cfg.architectures)
                    except BaseException:
                        path.unlink(missing_ok=True)
                        raise
                    return path, parsed

                try:
                    path, parsed = await run_in_threadpool(prepare_file)
                except DebParseError as exc:
                    emit("upload_reject", reason=str(exc), filename=name)
                    return RedirectResponse("/admin/packages?err=" + quote(str(exc), safe=""), status_code=303)
                except ValueError:
                    return HTMLResponse("上传超过大小限制", status_code=413)
                incoming_paths.append(path)
                total += parsed.size
                items.append((parsed, path))

        def work():
            disk_preflight(cfg.repo_dir, total)
            conn = connect(cfg)
            try:
                return upload_commit(cfg, conn, items, user_id=user.id)
            finally:
                conn.close()

        results, pub = await run_in_threadpool(work)
        if pub is not None and not pub.ok:
            return RedirectResponse("/admin/packages?err=upload_publish", status_code=303)
        if any(not r["ok"] and r.get("error") == "duplicate" for r in results) and not any(
            r["ok"] for r in results
        ):
            return RedirectResponse("/admin/packages?err=duplicate", status_code=409)
        return RedirectResponse("/admin/packages?ok=1", status_code=303)
    except DiskFullError:
        return HTMLResponse("磁盘空间不足", status_code=507)
    except HTTPException as exc:
        if exc.status_code == 400 and str(exc.detail).startswith("Too many files."):
            return RedirectResponse("/admin/packages?err=too_many", status_code=303)
        raise
    finally:
        for path in incoming_paths:
            path.unlink(missing_ok=True)


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
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
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
    if user.must_change_password:
        return RedirectResponse("/admin/password", status_code=303)
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
    result = await loop.run_in_executor(None, work)
    return RedirectResponse("/admin/?ok=1" if result.ok else "/admin/?err=publish", status_code=303)
