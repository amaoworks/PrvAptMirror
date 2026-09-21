"""Unauthenticated health endpoints. /healthz never takes publish.lock."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from prvaptmirror.db import connect, get_setting, last_publish_status
from prvaptmirror.settings import SETTINGS_PENDING_KEY

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request):
    cfg = request.state.cfg
    conn = connect(cfg)
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return PlainTextResponse("ok\n")


@router.get("/readyz")
def readyz(request: Request):
    cfg = request.state.cfg
    conn = connect(cfg)
    try:
        conn.execute("BEGIN")
        fpr = get_setting(conn, "gpg_fingerprint")
        dirty = get_setting(conn, "publish_dirty", "0") == "1"
        pending_settings = get_setting(conn, SETTINGS_PENDING_KEY, "0") == "1"
        last = last_publish_status(conn)
    finally:
        conn.close()
    inrelease = cfg.dists_dir / cfg.suite / "InRelease"
    if not fpr or not inrelease.is_file():
        return JSONResponse({"ready": False, "reason": "not published"}, status_code=503)
    if dirty or pending_settings or last != "success":
        return JSONResponse({"ready": False, "reason": "publish pending or failed"}, status_code=503)
    return JSONResponse({"ready": True, "fingerprint": fpr})
