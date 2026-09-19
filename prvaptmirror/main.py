"""ASGI entry: FastAPI app factory."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from prvaptmirror.auth import bootstrap_admin
from prvaptmirror.config import ensure_data_dirs, load_config, validate_startup
from prvaptmirror.db import connect, get_setting, init_db, set_setting
from prvaptmirror.events import emit
from prvaptmirror.filesystem import file_lock
from prvaptmirror.publish import publish_lock, publish_unlocked, startup_reconcile
from prvaptmirror.routes.admin import router as admin_router
from prvaptmirror.routes.health import router as health_router
from prvaptmirror.signing import SigningError, ensure_key
from prvaptmirror.source_sync import SourceScheduler
from prvaptmirror.storage import gc_incoming
from prvaptmirror.settings import SETTINGS_PENDING_KEY, ensure_app_settings, load_app_config

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    with file_lock(app.state.base_cfg.data_dir / "service.lock", shared=True, blocking=False):
        async with running_app(app):
            yield


@asynccontextmanager
async def running_app(app: FastAPI):
    base_cfg = app.state.base_cfg
    validate_startup(base_cfg)
    ensure_data_dirs(base_cfg)
    conn = None
    try:
        # Backups must not observe a database snapshot from before key creation
        # together with a keyring captured halfway through initialization.
        with publish_lock(base_cfg):
            conn = init_db(base_cfg)
            ensure_app_settings(conn, base_cfg)
            cfg = load_app_config(base_cfg, conn)
            app.state.cfg = cfg
            bootstrap_admin(cfg, conn)
            try:
                ensure_key(cfg, conn)
            except SigningError as exc:
                emit("gpg_bootstrap_fail", error=str(exc))
                raise
        if get_setting(conn, SETTINGS_PENDING_KEY, "0") == "1":
            with publish_lock(cfg):
                recovery = publish_unlocked(cfg, conn)
            if not recovery.ok:
                raise RuntimeError(f"pending settings recovery failed: {recovery.error}")
            set_setting(conn, SETTINGS_PENDING_KEY, "0")
        gc_incoming(cfg)
        startup_reconcile(cfg, conn)
    finally:
        if conn is not None:
            conn.close()
    scheduler = SourceScheduler(base_cfg)
    app.state.source_scheduler = scheduler
    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


def create_app(cfg=None) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="PrvAptMirror", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.base_cfg = cfg
    app.state.cfg = cfg

    @app.middleware("http")
    async def app_settings_snapshot(request, call_next):
        conn = connect(app.state.base_cfg)
        try:
            request.state.cfg = load_app_config(app.state.base_cfg, conn)
        finally:
            conn.close()
        return await call_next(request)

    app.include_router(health_router)
    app.include_router(admin_router, prefix="/admin")
    app.mount("/admin/static", StaticFiles(directory=str(STATIC_DIR)), name="admin-static")
    cfg.repo_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/apt", StaticFiles(directory=str(cfg.repo_dir), html=False), name="apt")

    @app.get("/")
    def root():
        return RedirectResponse("/admin/", status_code=303)

    return app


app = create_app()


def cli() -> None:
    import uvicorn

    uvicorn.run(
        "prvaptmirror.main:app",
        host=os.environ.get("PRVAPT_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PRVAPT_BIND_PORT", "8000")),
        workers=1,
        factory=False,
    )
