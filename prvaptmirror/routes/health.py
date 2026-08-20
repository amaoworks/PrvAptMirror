"""Unauthenticated health endpoints. /healthz never takes publish.lock."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from prvaptmirror.db import connect, get_setting

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request):
    cfg = request.app.state.cfg
    conn = connect(cfg)
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return PlainTextResponse("ok\n")


@router.get("/readyz")
def readyz(request: Request):
    cfg = request.app.state.cfg
    conn = connect(cfg)
    try:
        fpr = get_setting(conn, "gpg_fingerprint")
    finally:
        conn.close()
    inrelease = cfg.dists_dir / cfg.suite / "InRelease"
    if not fpr or not inrelease.is_file():
        return JSONResponse({"ready": False, "reason": "not published"}, status_code=503)
    return JSONResponse({"ready": True, "fingerprint": fpr})
