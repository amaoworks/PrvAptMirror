#!/bin/sh
set -eu
SRC="${1:-}"
DATA_DIR="${PRVAPT_HOST_DATA:-./data}"
if [ -z "$SRC" ]; then
  echo "usage: restore.sh BACKUP_DIR" >&2
  exit 2
fi
# Must stop writers before replacing the data dir.
if command -v docker >/dev/null 2>&1; then
  docker compose stop || true
fi
mkdir -p "$DATA_DIR"
cp "$SRC/data.sqlite" "$DATA_DIR/data.sqlite"
tar -C "$DATA_DIR" -xzf "$SRC/pool-and-meta.tgz"
sqlite3 "$DATA_DIR/data.sqlite" "PRAGMA integrity_check"
echo "restore complete; start the stack (docker compose start). startup will republish if dirty."
