from prvaptmirror.db import SCHEMA_VERSION, init_db


def test_migrate_empty(cfg):
    conn = init_db(cfg)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "packages" in tables
    assert "sessions" in tables
    assert "login_attempts" in tables
    dirty = conn.execute("SELECT value FROM settings WHERE key='publish_dirty'").fetchone()[0]
    assert dirty == "0"
    conn.close()
