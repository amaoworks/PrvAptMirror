from starlette.requests import Request

from prvaptmirror.auth import dummy_hash, hash_password, verify_password
from prvaptmirror.config import load_config
from prvaptmirror.csrf import origin_allowed, verify_csrf
from prvaptmirror.snippets import deb822_snippet, forbidden_in_snippet


def _request(origin: str | None = None) -> Request:
    headers = []
    if origin:
        headers.append((b"origin", origin.encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/admin/login",
        "raw_path": b"/admin/login",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8000),
    }
    return Request(scope)


def test_dummy_hash_is_valid_argon2():
    assert dummy_hash().startswith("$argon2")
    assert verify_password(hash_password("test-password-123"), "test-password-123")
    assert not verify_password(hash_password("test-password-123"), "nope")


def test_snippet_forbids_trusted_yes(cfg):
    text = deb822_snippet(cfg)
    assert "Signed-By:" in text
    assert not forbidden_in_snippet(text)
    assert "trusted=yes" not in text


def test_origin_check_off_by_default(cfg):
    assert cfg.origin_check is False
    assert origin_allowed(_request("http://evil.example"), cfg)
    assert origin_allowed(_request(None), cfg)
    assert verify_csrf(_request(None), cfg, "tok", "tok") is True


def test_env_password_syncs_existing_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("PRVAPT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRVAPT_SECRET_KEY", "s" * 48)
    monkeypatch.setenv("PRVAPT_ADMIN_USER", "admin")
    monkeypatch.setenv("PRVAPT_ADMIN_PASSWORD", "first-password-ok")
    from prvaptmirror.auth import authenticate, bootstrap_admin
    from prvaptmirror.db import init_db

    cfg = load_config()
    conn = init_db(cfg)
    bootstrap_admin(cfg, conn)
    assert authenticate(conn, "admin", "first-password-ok") is not None
    conn.close()

    monkeypatch.setenv("PRVAPT_ADMIN_PASSWORD", "second-password-ok")
    cfg2 = load_config()
    conn = init_db(cfg2)
    bootstrap_admin(cfg2, conn)
    assert authenticate(conn, "admin", "first-password-ok") is None
    assert authenticate(conn, "admin", "second-password-ok") is not None
    conn.close()


def test_origin_check_on_enforces_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("PRVAPT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRVAPT_SECRET_KEY", "s" * 48)
    monkeypatch.setenv("PRVAPT_PUBLIC_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("PRVAPT_ADMIN_ORIGINS", "http://127.0.0.1:8080")
    monkeypatch.setenv("PRVAPT_ORIGIN_CHECK", "1")
    cfg = load_config()
    assert cfg.origin_check is True
    assert origin_allowed(_request("http://127.0.0.1:8080"), cfg)
    assert not origin_allowed(_request("http://evil.example"), cfg)
    assert not origin_allowed(_request(None), cfg)
