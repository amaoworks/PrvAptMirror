from prvaptmirror.db import MIGRATIONS, SCHEMA_VERSION, init_db, migrate


def test_migrate_empty(cfg):
    conn = init_db(cfg)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "packages" in tables
    assert "sessions" in tables
    assert "login_attempts" in tables
    assert "package_sources" in tables
    assert "source_runs" in tables
    assert "source_artifacts" in tables
    dirty = conn.execute("SELECT value FROM settings WHERE key='publish_dirty'").fetchone()[0]
    assert dirty == "0"
    conn.close()


def test_migrate_existing_v1_database(cfg):
    import sqlite3

    conn = sqlite3.connect(cfg.db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(MIGRATIONS[1])
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO settings(key, value) VALUES ('preserved', 'yes')")
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("SELECT value FROM settings WHERE key='preserved'").fetchone()[0] == "yes"
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='package_sources'"
    ).fetchone()[0] == 1
    conn.close()
