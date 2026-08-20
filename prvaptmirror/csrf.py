"""CSRF: synchronizer token plus Origin/Referer allow-list (ADMIN_ORIGINS)."""

from __future__ import annotations

import hmac
import hashlib
import secrets
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import Response

from prvaptmirror.config import Config

COOKIE = "prvapt_csrf"
FORM_FIELD = "csrf_token"


def issue_anon_token() -> str:
    return secrets.token_urlsafe(32)


def session_token(secret: str, session_id_hash: str) -> str:
    return hmac.new(secret.encode("utf-8"), session_id_hash.encode("utf-8"), hashlib.sha256).hexdigest()


def request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    referer = request.headers.get("referer")
    if not referer:
        return None
    parsed = urlparse(referer)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def origin_allowed(request: Request, cfg: Config) -> bool:
    if not cfg.origin_check:
        return True
    origin = request_origin(request)
    if origin is None:
        return False
    return origin in cfg.admin_origins


def set_csrf_cookie(response: Response, token: str, *, secure: bool, max_age: int) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        path="/admin",
        secure=secure,
    )


def verify_csrf(request: Request, cfg: Config, form_token: str | None, expected: str) -> bool:
    if not origin_allowed(request, cfg):
        return False
    if not form_token or not expected:
        return False
    return hmac.compare_digest(form_token, expected)
