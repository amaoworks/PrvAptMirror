from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix


SESSION_COOKIE = "__Host-prvaptmirror_admin_session"
LOGIN_CSRF_COOKIE = "__Secure-prvaptmirror_login_csrf"
TOKEN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")


@dataclass
class SessionRecord:
    expires_at: float
    csrf_token: str


class SessionStore:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._records: dict[str, SessionRecord] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self) -> tuple[str, SessionRecord]:
        token = secrets.token_urlsafe(48)
        now = time.monotonic()
        record = SessionRecord(
            expires_at=now + self.ttl_seconds,
            csrf_token=secrets.token_urlsafe(32),
        )
        with self._lock:
            self._records = {
                key: value for key, value in self._records.items() if value.expires_at > now
            }
            self._records[self._digest(token)] = record
        return token, record

    def get(self, token: str | None) -> SessionRecord | None:
        if not token:
            return None
        digest = self._digest(token)
        now = time.monotonic()
        with self._lock:
            record = self._records.get(digest)
            if record is None:
                return None
            if record.expires_at <= now:
                self._records.pop(digest, None)
                return None
            record.expires_at = now + self.ttl_seconds
            return record

    def delete(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._records.pop(self._digest(token), None)


class LoginLimiter:
    def __init__(self, max_failures: int, window_seconds: int) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, values: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while values and values[0] <= cutoff:
            values.popleft()

    def allowed(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            values = self._failures[key]
            self._prune(values, now)
            if len(values) < self.max_failures:
                return True, 0
            retry_after = max(1, int(values[0] + self.window_seconds - now))
            return False, retry_after

    def fail(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            values = self._failures[key]
            self._prune(values, now)
            values.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def _read_password_hash(path_value: str) -> str:
    secret_path = Path(path_value)
    file_stat = secret_path.stat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise RuntimeError("管理员密码哈希必须是普通文件")
    if file_stat.st_uid != os.geteuid():
        raise RuntimeError("管理员密码哈希文件必须属于 admin-web 运行用户")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise RuntimeError("管理员密码哈希文件权限必须为 0600、0400 或更严格")
    value = secret_path.read_text(encoding="utf-8").strip()
    if not value.startswith("$argon2id$"):
        raise RuntimeError("管理员密码哈希必须使用 Argon2id")
    return value


def _validate_origin(origin: str, allow_insecure: bool) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme not in ({"http", "https"} if allow_insecure else {"https"}):
        raise RuntimeError("ADMIN_PUBLIC_ORIGIN 必须是 HTTPS Origin")
    if not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise RuntimeError("ADMIN_PUBLIC_ORIGIN 只能包含协议和主机名")
    return f"{parsed.scheme}://{parsed.netloc}"


def _repository_inventory(root: Path) -> list[dict[str, str | int]]:
    repositories: list[dict[str, str | int]] = []
    if not root.is_dir():
        return repositories

    for family_path in sorted(root.iterdir(), key=lambda item: item.name):
        if (
            family_path.is_symlink()
            or not family_path.is_dir()
            or not TOKEN_PATTERN.fullmatch(family_path.name)
        ):
            continue
        dists_path = family_path / "dists"
        if not dists_path.is_dir():
            continue
        for suite_path in sorted(dists_path.iterdir(), key=lambda item: item.name):
            if (
                suite_path.is_symlink()
                or not suite_path.is_dir()
                or not TOKEN_PATTERN.fullmatch(suite_path.name)
            ):
                continue
            inrelease = suite_path / "InRelease"
            if inrelease.is_symlink() or not inrelease.is_file():
                continue
            file_stat = inrelease.stat()
            updated = datetime.fromtimestamp(file_stat.st_mtime, tz=timezone.utc)
            repositories.append(
                {
                    "family": family_path.name,
                    "suite": suite_path.name,
                    "size": file_stat.st_size,
                    "updated": int(file_stat.st_mtime),
                    "updated_iso": updated.isoformat(),
                    "updated_label": updated.strftime("%Y-%m-%d %H:%M UTC"),
                }
            )
    return repositories


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        ADMIN_USERNAME=os.environ.get("ADMIN_USERNAME", "admin"),
        ADMIN_PASSWORD_HASH_FILE=os.environ.get(
            "ADMIN_PASSWORD_HASH_FILE", "/run/secrets/admin_password_hash"
        ),
        ADMIN_PASSWORD_HASH_FOR_TESTS=None,
        ADMIN_PUBLIC_ORIGIN=os.environ.get("ADMIN_PUBLIC_ORIGIN", ""),
        ADMIN_ALLOW_INSECURE_ORIGIN=os.environ.get("ADMIN_ALLOW_INSECURE_ORIGIN") == "1",
        ADMIN_SESSION_TTL=int(os.environ.get("ADMIN_SESSION_TTL", "28800")),
        ADMIN_LOGIN_MAX_FAILURES=int(os.environ.get("ADMIN_LOGIN_MAX_FAILURES", "5")),
        ADMIN_LOGIN_WINDOW=int(os.environ.get("ADMIN_LOGIN_WINDOW", "900")),
        REPOSITORY_ROOT=os.environ.get("REPOSITORY_ROOT", "/srv/repository"),
        MAX_CONTENT_LENGTH=65536,
    )
    if overrides:
        app.config.update(overrides)

    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", app.config["ADMIN_USERNAME"]):
        raise RuntimeError("ADMIN_USERNAME 格式无效")
    if not 300 <= app.config["ADMIN_SESSION_TTL"] <= 86400:
        raise RuntimeError("ADMIN_SESSION_TTL 必须在 300 到 86400 秒之间")
    if not 3 <= app.config["ADMIN_LOGIN_MAX_FAILURES"] <= 20:
        raise RuntimeError("ADMIN_LOGIN_MAX_FAILURES 必须在 3 到 20 之间")
    public_origin = _validate_origin(
        app.config["ADMIN_PUBLIC_ORIGIN"], app.config["ADMIN_ALLOW_INSECURE_ORIGIN"]
    )
    password_hash = app.config["ADMIN_PASSWORD_HASH_FOR_TESTS"] or _read_password_hash(
        app.config["ADMIN_PASSWORD_HASH_FILE"]
    )
    if not password_hash.startswith("$argon2id$"):
        raise RuntimeError("管理员密码哈希必须使用 Argon2id")

    password_hasher = PasswordHasher()
    try:
        password_hasher.check_needs_rehash(password_hash)
    except (VerificationError, ValueError) as exc:
        raise RuntimeError("管理员密码哈希无效") from exc
    sessions = SessionStore(app.config["ADMIN_SESSION_TTL"])
    limiter = LoginLimiter(
        app.config["ADMIN_LOGIN_MAX_FAILURES"], app.config["ADMIN_LOGIN_WINDOW"]
    )
    started_at = time.monotonic()

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    def client_key() -> str:
        return f"{request.remote_addr or 'unknown'}:{app.config['ADMIN_USERNAME']}"

    def origin_is_valid() -> bool:
        supplied = request.headers.get("Origin", "")
        return hmac.compare_digest(supplied, public_origin)

    def login_page(message: str | None = None, status: int = 200) -> Response:
        token = secrets.token_urlsafe(32)
        response = Response(
            render_template(
                "login.html",
                csrf_token=token,
                message=message,
                username=app.config["ADMIN_USERNAME"],
            ),
            status=status,
            mimetype="text/html",
        )
        response.set_cookie(
            LOGIN_CSRF_COOKIE,
            token,
            max_age=600,
            secure=True,
            httponly=True,
            samesite="Strict",
            path="/login",
        )
        return response

    def require_auth(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.admin_session is None:
                return redirect(url_for("login"), code=303)
            return view(*args, **kwargs)

        return wrapped

    def require_state_change_csrf() -> None:
        if not origin_is_valid():
            abort(403, "请求来源无效")
        supplied = request.form.get("csrf_token", "")
        expected = g.admin_session.csrf_token if g.admin_session else ""
        if not supplied or not hmac.compare_digest(supplied, expected):
            abort(400, "CSRF 校验失败")

    @app.before_request
    def load_session() -> None:
        g.admin_session = sessions.get(request.cookies.get(SESSION_COOKIE))

    @app.after_request
    def secure_response(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'; img-src 'self'; "
            "style-src 'self'; script-src 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=3600"
        else:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def healthz() -> Response:
        return Response("ok\n", mimetype="text/plain")

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response:
        if request.method == "GET":
            if g.admin_session is not None:
                return redirect(url_for("dashboard"), code=303)
            return login_page()

        if not origin_is_valid():
            abort(403, "请求来源无效")
        supplied_csrf = request.form.get("csrf_token", "")
        cookie_csrf = request.cookies.get(LOGIN_CSRF_COOKIE, "")
        if not supplied_csrf or not hmac.compare_digest(supplied_csrf, cookie_csrf):
            abort(400, "CSRF 校验失败")

        key = client_key()
        allowed, retry_after = limiter.allowed(key)
        if not allowed:
            response = login_page("登录尝试过多，请稍后再试。", 429)
            response.headers["Retry-After"] = str(retry_after)
            return response

        supplied_username = request.form.get("username", "")
        supplied_password = request.form.get("password", "")
        password_candidate = supplied_password if len(supplied_password) <= 1024 else ""
        password_valid = False
        try:
            password_valid = password_hasher.verify(password_hash, password_candidate)
        except (VerificationError, ValueError):
            password_valid = False
        username_valid = hmac.compare_digest(
            supplied_username, app.config["ADMIN_USERNAME"]
        )

        if not username_valid or not password_valid:
            limiter.fail(key)
            return login_page("账号或密码不正确。", 401)

        limiter.reset(key)
        token, _ = sessions.create()
        response = redirect(url_for("dashboard"), code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=app.config["ADMIN_SESSION_TTL"],
            secure=True,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        response.delete_cookie(LOGIN_CSRF_COOKIE, path="/login", secure=True, httponly=True)
        return response

    @app.post("/logout")
    @require_auth
    def logout() -> Response:
        require_state_change_csrf()
        token = request.cookies.get(SESSION_COOKIE)
        sessions.delete(token)
        response = redirect(url_for("login"), code=303)
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True)
        return response

    def dashboard_data() -> dict:
        repository_root = Path(app.config["REPOSITORY_ROOT"])
        repositories = _repository_inventory(repository_root)
        public_key = repository_root / "repository-key.gpg"
        return {
            "repositories": repositories,
            "repository_count": len(repositories),
            "public_key_ready": public_key.is_file() and public_key.stat().st_size > 0,
            "uptime_seconds": int(time.monotonic() - started_at),
            "admin_username": app.config["ADMIN_USERNAME"],
        }

    @app.get("/")
    @require_auth
    def dashboard() -> Response:
        return Response(
            render_template(
                "dashboard.html",
                data=dashboard_data(),
                csrf_token=g.admin_session.csrf_token,
            ),
            mimetype="text/html",
        )

    @app.get("/api/status")
    @require_auth
    def api_status() -> Response:
        return jsonify(dashboard_data())

    return app
