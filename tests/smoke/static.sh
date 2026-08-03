#!/usr/bin/env bash

set -Eeuo pipefail

readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

bash -n "${PROJECT_ROOT}/containers/repoctl/repoctl"
bash -n "${PROJECT_ROOT}/containers/repo-worker/worker"
bash -n "${PROJECT_ROOT}/scripts/prvaptmirror"
bash -n "${PROJECT_ROOT}/tests/smoke/run.sh"
bash -n "${PROJECT_ROOT}/tests/smoke/admin-setup.sh"
bash -n "${PROJECT_ROOT}/tests/smoke/repoctl-local.sh"
python3 -c 'import ast,sys; [ast.parse(open(path, encoding="utf-8").read(), filename=path) for path in sys.argv[1:]]' \
    "${PROJECT_ROOT}/containers/admin-web/app.py" \
    "${PROJECT_ROOT}/containers/admin-web/manage.py" \
    "${PROJECT_ROOT}/containers/admin-web/wsgi.py" \
    "${PROJECT_ROOT}/containers/admin-web/tests/test_app.py"

if command -v python3 >/dev/null 2>&1; then
    python3 -m json.tool "${PROJECT_ROOT}/config/aptly/aptly.conf" >/dev/null
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose \
        -f "${PROJECT_ROOT}/compose.yaml" \
        --profile "*" \
        config --quiet
fi

printf '静态检查通过\n'
