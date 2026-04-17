#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRIMARY_IMAGE="${1:-}"
SECONDARY_IMAGE="${2:-}"
DOCKER_PREFIX=""

source "${ROOT_DIR}/scripts/docker_common.sh"

usage() {
  cat <<'EOF'
Usage:
  ./publish.sh <image-ref> [additional-tag]

Example:
  ./publish.sh ghcr.io/acme/streaming-video-server:0.1.0 ghcr.io/acme/streaming-video-server:latest
EOF
}

if [[ -z "${PRIMARY_IMAGE}" ]]; then
  usage >&2
  exit 1
fi

require_cmd rsync
check_docker_access

"${ROOT_DIR}/scripts/sync_soulx_vendor.sh"

docker_cmd build -t "${PRIMARY_IMAGE}" -f "${ROOT_DIR}/Dockerfile" "${ROOT_DIR}"
docker_cmd push "${PRIMARY_IMAGE}"

if [[ -n "${SECONDARY_IMAGE}" ]]; then
  docker_cmd tag "${PRIMARY_IMAGE}" "${SECONDARY_IMAGE}"
  docker_cmd push "${SECONDARY_IMAGE}"
fi

cat <<EOF
Published image:
  ${PRIMARY_IMAGE}
EOF

if [[ -n "${SECONDARY_IMAGE}" ]]; then
  cat <<EOF
Additional tag:
  ${SECONDARY_IMAGE}
EOF
fi
