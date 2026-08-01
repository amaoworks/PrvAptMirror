#!/usr/bin/env bash

set -Eeuo pipefail

readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

bash -n "${PROJECT_ROOT}/containers/repoctl/repoctl"
bash -n "${PROJECT_ROOT}/scripts/prvaptmirror"
bash -n "${PROJECT_ROOT}/tests/smoke/run.sh"
bash -n "${PROJECT_ROOT}/tests/smoke/repoctl-local.sh"

if command -v python3 >/dev/null 2>&1; then
    python3 -m json.tool "${PROJECT_ROOT}/config/aptly/aptly.conf" >/dev/null
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose \
        -f "${PROJECT_ROOT}/deploy/compose/compose.yaml" \
        --profile tools \
        config --quiet
fi

printf '静态检查通过\n'
