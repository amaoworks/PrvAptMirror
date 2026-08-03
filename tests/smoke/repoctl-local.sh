#!/usr/bin/env bash

set -Eeuo pipefail

readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REPOCTL="${PROJECT_ROOT}/containers/repoctl/repoctl"
readonly APTLY_BINARY="${1:-$(command -v aptly || true)}"
readonly TEST_ROOT="$(mktemp -d -t prvaptmirror-repoctl.XXXXXXXX)"

cleanup() {
    case "${TEST_ROOT}" in
        /tmp/prvaptmirror-repoctl.*) rm -rf -- "${TEST_ROOT}" ;;
    esac
}
trap cleanup EXIT

[[ -n "${APTLY_BINARY}" && -x "${APTLY_BINARY}" ]] || {
    printf '用法：%s /path/to/aptly\n' "$0" >&2
    exit 1
}

mkdir -p \
    "${TEST_ROOT}/bin" \
    "${TEST_ROOT}/incoming" \
    "${TEST_ROOT}/data/aptly" \
    "${TEST_ROOT}/data/gnupg" \
    "${TEST_ROOT}/data/public" \
    "${TEST_ROOT}/state"
ln -s "${APTLY_BINARY}" "${TEST_ROOT}/bin/aptly"

sed \
    -e "s#/var/lib/aptly#${TEST_ROOT}/data/aptly#g" \
    -e "s#/var/public#${TEST_ROOT}/data/public#g" \
    "${PROJECT_ROOT}/config/aptly/aptly.conf" \
    >"${TEST_ROOT}/aptly.conf"

make_test_deb() {
    local package_name="$1"
    local version="$2"
    local architecture="$3"
    local build_root="${TEST_ROOT}/build-${package_name}-${version}-${architecture}"
    local output="${TEST_ROOT}/incoming/${package_name}_${version}_${architecture}.deb"

    mkdir -p "${build_root}/DEBIAN" "${build_root}/usr/share/${package_name}"
    printf '%s\n' \
        "Package: ${package_name}" \
        "Version: ${version}" \
        'Section: misc' \
        'Priority: optional' \
        "Architecture: ${architecture}" \
        'Maintainer: PrvAptMirror Test <test@example.invalid>' \
        'Description: PrvAptMirror local repository test package' \
        >"${build_root}/DEBIAN/control"
    printf '%s\n' "${version}" >"${build_root}/usr/share/${package_name}/version"
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

repoctl() {
    PATH="${TEST_ROOT}/bin:${PATH}" \
    APTLY_CONFIG="${TEST_ROOT}/aptly.conf" \
    APTLY_ROOT="${TEST_ROOT}/data/aptly" \
    GNUPGHOME="${TEST_ROOT}/data/gnupg" \
    INCOMING_ROOT="${TEST_ROOT}/incoming" \
    PUBLIC_ROOT="${TEST_ROOT}/data/public" \
    STATE_ROOT="${TEST_ROOT}/state" \
    REPO_GPG_NAME="PrvAptMirror Repoctl Test" \
    REPO_GPG_EMAIL="test@example.test" \
    REPO_GPG_EXPIRE="1d" \
        "${REPOCTL}" "$@"
}

make_test_deb prvaptmirror-test 1.0.0 amd64
make_test_deb prvaptmirror-test 2.0.0 amd64
make_test_deb prvaptmirror-arm64 1.0.0 arm64
make_test_deb prvaptmirror-armhf 1.0.0 armhf
make_test_deb prvaptmirror-common 1.0.0 all

repoctl init
repoctl repo create ubuntu noble
repoctl package add ubuntu noble "${TEST_ROOT}/incoming/prvaptmirror-test_1.0.0_amd64.deb"
repoctl package add ubuntu noble "${TEST_ROOT}/incoming/prvaptmirror-arm64_1.0.0_arm64.deb"
repoctl package add ubuntu noble "${TEST_ROOT}/incoming/prvaptmirror-armhf_1.0.0_armhf.deb"
repoctl package add ubuntu noble "${TEST_ROOT}/incoming/prvaptmirror-common_1.0.0_all.deb"

gpgv \
    --keyring "${TEST_ROOT}/data/public/repository-key.gpg" \
    "${TEST_ROOT}/data/public/ubuntu/dists/noble/InRelease"
package_index_contains \
    "${TEST_ROOT}/data/public/ubuntu/dists/noble/main/binary-amd64/Packages.gz" \
    prvaptmirror-test 1.0.0
package_index_contains \
    "${TEST_ROOT}/data/public/ubuntu/dists/noble/main/binary-arm64/Packages.gz" \
    prvaptmirror-arm64 1.0.0
package_index_contains \
    "${TEST_ROOT}/data/public/ubuntu/dists/noble/main/binary-armhf/Packages.gz" \
    prvaptmirror-armhf 1.0.0
for architecture in amd64 arm64 armhf; do
    package_index_contains \
        "${TEST_ROOT}/data/public/ubuntu/dists/noble/main/binary-${architecture}/Packages.gz" \
        prvaptmirror-common 1.0.0
done

repoctl package add ubuntu noble "${TEST_ROOT}/incoming/prvaptmirror-test_2.0.0_amd64.deb"
package_index_contains \
    "${TEST_ROOT}/data/public/ubuntu/dists/noble/main/binary-amd64/Packages.gz" \
    prvaptmirror-test 2.0.0
repoctl rollback ubuntu noble
package_index_contains \
    "${TEST_ROOT}/data/public/ubuntu/dists/noble/main/binary-amd64/Packages.gz" \
    prvaptmirror-test 1.0.0

printf 'repoctl 本地真实链路测试通过\n'
