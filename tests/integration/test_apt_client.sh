#!/bin/sh
# Talks to the app HTTP port. Skip (exit 2) if docker/apt images cannot run.
set -eu
BASE="${PRVAPT_PUBLIC_URL:-http://127.0.0.1:8000}"
if ! command -v docker >/dev/null 2>&1; then
  echo "docker unavailable" >&2
  exit 2
fi
if ! curl -fsS "$BASE/healthz" >/dev/null; then
  echo "healthz not reachable at $BASE" >&2
  exit 2
fi
IMG="${APT_CLIENT_IMAGE:-debian:bookworm}"
docker run --rm --network host "$IMG" bash -s -- "$BASE" <<'EOS'
set -euo pipefail
BASE="$1"
install -d -m 0755 /etc/apt/keyrings
apt-get update -qq
apt-get install -y -qq curl ca-certificates >/dev/null
curl -fsSL "$BASE/apt/pubkey.asc" -o /etc/apt/keyrings/prvaptmirror.asc
cat >/etc/apt/sources.list.d/prvaptmirror.sources <<EOF
Types: deb
URIs: $BASE/apt
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/prvaptmirror.asc
EOF
apt-get update
apt-cache policy || true
EOS
