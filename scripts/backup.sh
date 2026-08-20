#!/bin/sh
set -eu
DATA_DIR="${PRVAPT_DATA_DIR:-/var/lib/prvaptmirror}"
DEST="${1:-}"
if [ -z "$DEST" ]; then
  echo "usage: backup.sh DEST_DIR" >&2
  exit 2
fi
mkdir -p "$DEST"
sqlite3 "$DATA_DIR/data.sqlite" ".backup $DEST/data.sqlite"
tar --exclude=repo/dists -C "$DATA_DIR" -czf "$DEST/pool-and-meta.tgz" repo/pool gnupg
{
  echo "packages=$(sqlite3 "$DATA_DIR/data.sqlite" "select count(*) from packages")"
  echo "fingerprint=$(sqlite3 "$DATA_DIR/data.sqlite" "select value from settings where key='gpg_fingerprint'")"
  sqlite3 "$DATA_DIR/data.sqlite" "select sha256, filename from packages order by filename"
} > "$DEST/manifest.txt"
echo "backup written to $DEST"
