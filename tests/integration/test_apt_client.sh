#!/bin/sh
# Talks to the app HTTP port. Skip (exit 2) if Docker or the app is unavailable.
set -eu
BASE="${PRVAPT_PUBLIC_URL:-http://127.0.0.1:8000}"
if ! command -v docker >/dev/null 2>&1; then
  echo "docker unavailable" >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "docker daemon unavailable" >&2
  exit 2
fi
if ! curl -fsS "$BASE/readyz" >/dev/null; then
  echo "repository not ready at $BASE" >&2
  exit 2
fi
IMG="${APT_CLIENT_IMAGE:-debian:bookworm}"
docker run --rm -i --network host "$IMG" bash -s -- "$BASE" \
  "${APT_TEST_SUITE:-stable}" "${APT_TEST_COMPONENT:-main}" <<'EOS'
set -euo pipefail
BASE="$1"
SUITE="$2"
COMPONENT="$3"
install -d -m 0755 /etc/apt/keyrings
apt-get -o APT::Update::Error-Mode=any update -qq
apt-get install -y -qq curl ca-certificates >/dev/null
curl -fsSL "$BASE/apt/pubkey.asc" -o /etc/apt/keyrings/prvaptmirror.asc
cat >/etc/apt/sources.list.d/prvaptmirror.sources <<EOF
Types: deb
URIs: $BASE/apt
Suites: $SUITE
Components: $COMPONENT
Signed-By: /etc/apt/keyrings/prvaptmirror.asc
By-Hash: force
EOF
apt-get -o APT::Update::Error-Mode=any update
apt-cache policy
echo "APT integration passed: signed repository indexes fetched via By-Hash"
EOS
