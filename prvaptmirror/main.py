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
from prvaptmirror.db import connect, init_db
from prvaptmirror.events import emit
from prvaptmirror.publish import startup_reconcile
from prvaptmirror.routes.admin import router as admin_router
from prvaptmirror.routes.health import router as health_router
from prvaptmirror.signing import SigningError, ensure_key
from prvaptmirror.storage import gc_incoming

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = app.state.cfg
    validate_startup(cfg)
    ensure_data_dirs(cfg)
    conn = init_db(cfg)
    try:
        bootstrap_admin(cfg, conn)
        try:
            ensure_key(cfg, conn)
        except SigningError as exc:
            emit("gpg_bootstrap_fail", error=str(exc))
            raise
        gc_incoming(cfg)
        startup_reconcile(cfg, conn)
    finally:
        conn.close()
    yield


def create_app(cfg=None) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="PrvAptMirror", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.cfg = cfg
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
