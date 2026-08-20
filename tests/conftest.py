from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ORIGIN = "http://127.0.0.1:8080"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("PRVAPT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRVAPT_SECRET_KEY", "s" * 48)
    monkeypatch.setenv("PRVAPT_ADMIN_USER", "admin")
    monkeypatch.setenv("PRVAPT_ADMIN_PASSWORD", "test-password-123")
    monkeypatch.setenv("PRVAPT_PUBLIC_URL", ORIGIN)
    monkeypatch.setenv("PRVAPT_ADMIN_ORIGINS", ORIGIN)
    monkeypatch.setenv("PRVAPT_COOKIE_SECURE", "false")
    from prvaptmirror.config import ensure_data_dirs, load_config

    conf = load_config()
    ensure_data_dirs(conf)
    return conf


@pytest.fixture
def ready(cfg):
    from prvaptmirror.auth import bootstrap_admin
    from prvaptmirror.db import init_db
    from prvaptmirror.signing import ensure_key

    conn = init_db(cfg)
    bootstrap_admin(cfg, conn)
    ensure_key(cfg, conn)
    conn.close()
    return cfg


@pytest.fixture
def client(ready):
    from prvaptmirror.main import create_app

    app = create_app(ready)
    with TestClient(app, base_url=ORIGIN) as test_client:
        yield test_client
