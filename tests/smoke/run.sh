#!/usr/bin/env bash

set -Eeuo pipefail

readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly COMPOSE_FILE="${PROJECT_ROOT}/deploy/compose/compose.yaml"
readonly TEST_ROOT="$(mktemp -d -t prvaptmirror-smoke.XXXXXXXX)"
readonly TEST_PROJECT="prvaptmirror-smoke-$RANDOM"
readonly TEST_PORT="${PRVAPTMIRROR_TEST_PORT:-18080}"

compose() {
    REPO_RUNTIME_DIR="${TEST_ROOT}/var" \
    REPO_HTTP_BIND=127.0.0.1 \
    REPO_HTTP_PORT="${TEST_PORT}" \
    REPO_GPG_NAME="PrvAptMirror Smoke Test" \
    REPO_GPG_EMAIL="smoke@example.invalid" \
        docker compose -p "${TEST_PROJECT}" -f "${COMPOSE_FILE}" "$@"
}

cleanup() {
    compose down --remove-orphans >/dev/null 2>&1 || true
    case "${TEST_ROOT}" in
        /tmp/prvaptmirror-smoke.*) rm -rf -- "${TEST_ROOT}" ;;
    esac
}
trap cleanup EXIT

make_test_deb() {
    local package_name="$1"
    local version="$2"
    local architecture="$3"
    local build_root="${TEST_ROOT}/build-${package_name}-${version}-${architecture}"
    local output="${TEST_ROOT}/var/incoming/${package_name}_${version}_${architecture}.deb"

    mkdir -p "${build_root}/DEBIAN" "${build_root}/usr/share/prvaptmirror-smoke"
    printf '%s\n' \
        "Package: ${package_name}" \
        "Version: ${version}" \
        'Section: misc' \
        'Priority: optional' \
        "Architecture: ${architecture}" \
        'Maintainer: PrvAptMirror Smoke Test <smoke@example.invalid>' \
        'Description: PrvAptMirror end-to-end smoke test package' \
        >"${build_root}/DEBIAN/control"
    printf '%s\n' "${version}" \
        >"${build_root}/usr/share/prvaptmirror-smoke/version"
    dpkg-deb --build --root-owner-group "${build_root}" "${output}" >/dev/null
}

package_index_contains() {
    local index_path="$1"
    local package_name="$2"
    local package_version="$3"

    gzip -dc "${index_path}" | awk \
        -v expected_package="${package_name}" \
        -v expected_version="${package_version}" '
            $1 == "Package:" { current_package = $2 }
            $1 == "Version:" && current_package == expected_package && $2 == expected_version { found = 1 }
            END { exit(found ? 0 : 1) }
        '
}

command -v docker >/dev/null 2>&1 || {
    printf '错误：端到端测试需要 Docker\n' >&2
    exit 1
}
docker compose version >/dev/null 2>&1 || {
    printf '错误：端到端测试需要 Docker Compose 插件\n' >&2
    exit 1
}
command -v dpkg-deb >/dev/null 2>&1 || {
    printf '错误：端到端测试需要 dpkg-deb\n' >&2
    exit 1
}
command -v gpgv >/dev/null 2>&1 || {
    printf '错误：端到端测试需要 gpgv\n' >&2
    exit 1
}
command -v gzip >/dev/null 2>&1 || {
    printf '错误：端到端测试需要 gzip\n' >&2
    exit 1
}
command -v curl >/dev/null 2>&1 || {
    printf '错误：端到端测试需要 curl\n' >&2
    exit 1
}

mkdir -p \
    "${TEST_ROOT}/var/incoming" \
    "${TEST_ROOT}/var/lib/aptly" \
    "${TEST_ROOT}/var/lib/gnupg" \
    "${TEST_ROOT}/var/public"

make_test_deb prvaptmirror-smoke 1.0.0 amd64
make_test_deb prvaptmirror-smoke 2.0.0 amd64
make_test_deb prvaptmirror-arm64 1.0.0 arm64
make_test_deb prvaptmirror-armhf 1.0.0 armhf
make_test_deb prvaptmirror-common 1.0.0 all

compose build repoctl repo-web
compose run --rm repoctl init
compose run --rm repoctl repo create ubuntu noble
compose run --rm repoctl package add ubuntu noble /incoming/prvaptmirror-smoke_1.0.0_amd64.deb
compose run --rm repoctl package add ubuntu noble /incoming/prvaptmirror-arm64_1.0.0_arm64.deb
compose run --rm repoctl package add ubuntu noble /incoming/prvaptmirror-armhf_1.0.0_armhf.deb
compose run --rm repoctl package add ubuntu noble /incoming/prvaptmirror-common_1.0.0_all.deb

test -s "${TEST_ROOT}/var/public/repository-key.gpg"
test -s "${TEST_ROOT}/var/public/ubuntu/dists/noble/InRelease"
gpgv \
    --keyring "${TEST_ROOT}/var/public/repository-key.gpg" \
    "${TEST_ROOT}/var/public/ubuntu/dists/noble/InRelease"
package_index_contains \
    "${TEST_ROOT}/var/public/ubuntu/dists/noble/main/binary-amd64/Packages.gz" \
    prvaptmirror-smoke \
    1.0.0
gzip -dc "${TEST_ROOT}/var/public/ubuntu/dists/noble/main/binary-arm64/Packages.gz" \
    | grep -q '^Package: prvaptmirror-arm64$'
gzip -dc "${TEST_ROOT}/var/public/ubuntu/dists/noble/main/binary-armhf/Packages.gz" \
    | grep -q '^Package: prvaptmirror-armhf$'
for architecture in amd64 arm64 armhf; do
    gzip -dc "${TEST_ROOT}/var/public/ubuntu/dists/noble/main/binary-${architecture}/Packages.gz" \
        | grep -q '^Package: prvaptmirror-common$'
done

compose run --rm repoctl package add ubuntu noble /incoming/prvaptmirror-smoke_2.0.0_amd64.deb
package_index_contains \
    "${TEST_ROOT}/var/public/ubuntu/dists/noble/main/binary-amd64/Packages.gz" \
    prvaptmirror-smoke \
    2.0.0

compose run --rm repoctl rollback ubuntu noble
package_index_contains \
    "${TEST_ROOT}/var/public/ubuntu/dists/noble/main/binary-amd64/Packages.gz" \
    prvaptmirror-smoke \
    1.0.0

compose up -d repo-web
curl --fail --silent --show-error "http://127.0.0.1:${TEST_PORT}/healthz" \
    | grep -qx ok
curl --fail --silent --show-error \
    "http://127.0.0.1:${TEST_PORT}/ubuntu/dists/noble/InRelease" \
    >/dev/null

printf '端到端冒烟测试通过\n'
