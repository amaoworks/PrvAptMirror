from __future__ import annotations

import argparse
import os
import sys

from app import AuthState


def show_setup_token() -> int:
    auth_state = AuthState(os.environ.get("ADMIN_AUTH_ROOT", "/data/admin/auth"))
    if auth_state.password_hash() is not None:
        print("管理员首次设置已经完成。")
        return 0
    token = auth_state.setup_token()
    if token is None:
        print("错误：没有找到一次性设置令牌，请先启动完整服务。", file=sys.stderr)
        return 1
    public_origin = os.environ.get("ADMIN_PUBLIC_ORIGIN", "").rstrip("/")
    path_prefix = os.environ.get("ADMIN_PATH_PREFIX", "/admin").strip()
    if path_prefix and not path_prefix.startswith("/"):
        path_prefix = f"/{path_prefix}"
    path_prefix = path_prefix.rstrip("/")
    if public_origin:
        print(f"打开 {public_origin}{path_prefix}/setup")
    print("在管理页面的首次设置表单中输入以下一次性令牌：")
    print(token)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PrvAptMirror 管理工具")
    parser.add_argument("command", choices=["setup-token"])
    args = parser.parse_args()
    if args.command == "setup-token":
        return show_setup_token()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
