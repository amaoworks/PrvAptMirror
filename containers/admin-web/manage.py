from __future__ import annotations

import argparse
import getpass
import sys

from argon2 import PasswordHasher


def hash_password() -> int:
    password = getpass.getpass("管理员密码：")
    confirmation = getpass.getpass("再次输入：")
    if password != confirmation:
        print("错误：两次密码不一致", file=sys.stderr)
        return 1
    if len(password) < 14:
        print("错误：密码至少需要 14 个字符", file=sys.stderr)
        return 1
    print(PasswordHasher().hash(password))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PrvAptMirror 管理工具")
    parser.add_argument("command", choices=["hash-password"])
    args = parser.parse_args()
    if args.command == "hash-password":
        return hash_password()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
