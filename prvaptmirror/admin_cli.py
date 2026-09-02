"""Recovery-only administrator commands."""

from __future__ import annotations

import argparse
import getpass

from prvaptmirror.auth import change_password
from prvaptmirror.config import load_config
from prvaptmirror.db import get_user_by_username, init_db


def reset_password(cfg, username: str, password: str) -> None:
    if len(password) < 10:
        raise ValueError("password must contain at least 10 characters")
    conn = init_db(cfg)
    try:
        user = get_user_by_username(conn, username)
        if user is None:
            raise ValueError(f"administrator not found: {username}")
        change_password(conn, user, password)
        conn.execute("DELETE FROM login_attempts")
        cfg.bootstrap_path.unlink(missing_ok=True)
    finally:
        conn.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="PrvAptMirror administrator recovery")
    subparsers = parser.add_subparsers(dest="command", required=True)
    reset = subparsers.add_parser("reset-password", help="reset an administrator password")
    reset.add_argument("--username", default="admin")
    args = parser.parse_args()

    password = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        parser.error("passwords do not match")
    try:
        reset_password(load_config(), args.username, password)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Password reset for {args.username}; existing sessions were revoked.")
