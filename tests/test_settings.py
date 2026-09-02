from __future__ import annotations

from dataclasses import replace

import pytest

from prvaptmirror.db import init_db
from prvaptmirror.settings import (
    SettingsValidationError,
    app_setting_values,
    config_from_app_values,
    ensure_app_settings,
    load_app_config,
    save_app_config,
)


def test_settings_are_seeded_once_and_then_owned_by_database(cfg):
    conn = init_db(cfg)
    ensure_app_settings(conn, cfg)
    values = app_setting_values(cfg)
    values["public_url"] = "https://apt.example.com"
    changed = config_from_app_values(cfg, values)
    save_app_config(conn, changed)

    different_environment = replace(cfg, public_url="https://environment.example")
    ensure_app_settings(conn, different_environment)
    loaded = load_app_config(different_environment, conn)
    conn.close()

    assert loaded.public_url == "https://apt.example.com"
    assert loaded.architectures == ("amd64", "arm64", "all")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_url", "file:///tmp/repo"),
        ("public_url", "https://example.com/bad path"),
        ("public_url", "https://example.com:99999"),
        ("public_url", "https://example.com/$(touch-pwned)"),
        ("suite", "../escape"),
        ("component", "main/extra"),
        ("architectures", "amd64,../arm64"),
        ("max_upload_mb", "0"),
        ("max_upload_files", "101"),
        ("session_days", "366"),
    ],
)
def test_invalid_settings_are_rejected(cfg, field, value):
    values = app_setting_values(cfg)
    values[field] = value
    with pytest.raises(SettingsValidationError):
        config_from_app_values(cfg, values)
