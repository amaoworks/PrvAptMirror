#!/usr/bin/env bash

set -Eeuo pipefail

readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly COMPOSE_FILE="${PROJECT_ROOT}/compose.yaml"
readonly TEST_ROOT="$(mktemp -d -t prvaptmirror-admin-setup.XXXXXXXX)"
readonly TEST_PROJECT="prvaptmirror-admin-setup-$RANDOM"
readonly ORIGIN="http://127.0.0.1"
readonly PASSWORD="PrvAptMirror setup test password 2026!"

compose() {
    PRVAPTMIRROR_DATA_DIR="${TEST_ROOT}/data" \
    REPO_HTTP_BIND=127.0.0.1 \
    REPO_HTTP_PORT=0 \
    ADMIN_HTTP_BIND=127.0.0.1 \
    ADMIN_HTTP_PORT=0 \
    ADMIN_PUBLIC_ORIGIN="${ORIGIN}" \
    ADMIN_ALLOW_INSECURE_ORIGIN=1 \
    REPO_GPG_NAME="PrvAptMirror Admin Setup Test" \
    REPO_GPG_EMAIL="admin-setup@example.test" \
    REPO_GPG_EXPIRE=1d \
        docker compose -p "${TEST_PROJECT}" -f "${COMPOSE_FILE}" "$@"
}

cleanup() {
    compose down --remove-orphans >/dev/null 2>&1 || true
    case "${TEST_ROOT}" in
        /tmp/prvaptmirror-admin-setup.*) rm -rf -- "${TEST_ROOT}" ;;
    esac
}
trap cleanup EXIT

csrf_from() {
    sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' "$1" | head -n 1
}

cookie_from_jar() {
    local jar="$1"
    local name="$2"
    awk -v expected="${name}" '$6 == expected { print $6 "=" $7; exit }' "${jar}"
}

compose up -d --build

admin_address="$(compose port admin-web 8081)"
admin_port="${admin_address##*:}"
repo_address="$(compose port repo-web 8080)"
repo_port="${repo_address##*:}"
[[ "${admin_port}" =~ ^[0-9]+$ && "${repo_port}" =~ ^[0-9]+$ ]]

admin_base="http://127.0.0.1:${admin_port}"
for _ in $(seq 1 30); do
    if curl --fail --silent "${admin_base}/healthz" >/dev/null; then
        break
    fi
    sleep 1
done
curl --fail --silent "${admin_base}/healthz" | grep -qx ok

setup_output="$(compose exec -T admin-web python3 /app/manage.py setup-token)"
grep -q '/setup' <<<"${setup_output}"
setup_token="$(tail -n 1 <<<"${setup_output}")"
[[ "${setup_token}" =~ ^[0-9a-f]{64}$ ]]

setup_html="${TEST_ROOT}/setup.html"
setup_cookies="${TEST_ROOT}/setup.cookies"
curl --fail --silent --cookie-jar "${setup_cookies}" \
    "${admin_base}/setup" >"${setup_html}"
setup_csrf="$(csrf_from "${setup_html}")"
setup_cookie="$(cookie_from_jar "${setup_cookies}" "__Secure-prvaptmirror_setup_csrf")"
[[ -n "${setup_csrf}" && -n "${setup_cookie}" ]]

setup_status="$(
    curl --silent --output /dev/null --write-out '%{http_code}' \
        --request POST \
        --header "Origin: ${ORIGIN}" \
        --header "Cookie: ${setup_cookie}" \
        --data-urlencode "csrf_token=${setup_csrf}" \
        --data-urlencode "setup_token=${setup_token}" \
        --data-urlencode "password=${PASSWORD}" \
        --data-urlencode "password_confirmation=${PASSWORD}" \
        "${admin_base}/setup"
)"
[[ "${setup_status}" == "303" ]]
test ! -e "${TEST_ROOT}/data/admin/auth/setup-token"
grep -q '^\$argon2id\$' "${TEST_ROOT}/data/admin/auth/password-hash"
grep -q '"action":"admin.setup"' "${TEST_ROOT}/data/admin/audit/events.jsonl"

login_html="${TEST_ROOT}/login.html"
login_cookies="${TEST_ROOT}/login.cookies"
curl --fail --silent --cookie-jar "${login_cookies}" \
    "${admin_base}/login" >"${login_html}"
login_csrf="$(csrf_from "${login_html}")"
login_cookie="$(cookie_from_jar "${login_cookies}" "__Secure-prvaptmirror_login_csrf")"
[[ -n "${login_csrf}" && -n "${login_cookie}" ]]

session_cookies="${TEST_ROOT}/session.cookies"
login_status="$(
    curl --silent --output /dev/null --write-out '%{http_code}' \
        --cookie-jar "${session_cookies}" \
        --request POST \
        --header "Origin: ${ORIGIN}" \
        --header "Cookie: ${login_cookie}" \
        --data-urlencode "csrf_token=${login_csrf}" \
        --data-urlencode 'username=admin' \
        --data-urlencode "password=${PASSWORD}" \
        "${admin_base}/login"
)"
[[ "${login_status}" == "303" ]]
session_cookie="$(cookie_from_jar "${session_cookies}" "__Host-prvaptmirror_admin_session")"
[[ -n "${session_cookie}" ]]
dashboard_html="${TEST_ROOT}/dashboard.html"
curl --fail --silent --header "Cookie: ${session_cookie}" "${admin_base}/" \
    >"${dashboard_html}"
grep -q 'CHECKPOINT 02' "${dashboard_html}"

[[ "$(curl --silent --output /dev/null --write-out '%{http_code}' "${admin_base}/setup")" == "404" ]]
curl --fail --silent "http://127.0.0.1:${repo_port}/repository-key.gpg" >/dev/null
[[ -n "$(compose ps --status running --quiet repo-worker)" ]]

printf '管理端首次设置端到端测试通过\n'
