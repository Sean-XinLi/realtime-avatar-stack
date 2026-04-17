#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${STREAM_ENV_FILE:-${ROOT_DIR}/.env.docker}"
ACTION="${1:-up}"
DOCKER_PREFIX=""

source "${ROOT_DIR}/scripts/docker_common.sh"


compose() {
  docker_cmd compose \
    --env-file "${ENV_FILE}" \
    -f "${ROOT_DIR}/docker-compose.yml" \
    -f "${ROOT_DIR}/docker-compose.build.yml" \
    "$@"
}

check_runtime() {
  require_cmd rsync
  check_linux_host
  check_docker_access
  check_compose_access
  check_nvidia_runtime
}

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${ROOT_DIR}/.env.docker.example" "${ENV_FILE}"
  echo "Created ${ENV_FILE}. Review it if you need to change defaults."
fi

case "${ACTION}" in
  up)
    check_runtime
    "${ROOT_DIR}/scripts/sync_soulx_vendor.sh"
    compose build streaming-video-server model-init
    compose run --rm model-init
    compose up -d streaming-video-server
    wait_for_health "${ENV_FILE}"
    compose ps
    ;;
  init-models)
    check_runtime
    "${ROOT_DIR}/scripts/sync_soulx_vendor.sh"
    compose build model-init
    compose run --rm model-init
    ;;
  build)
    check_runtime
    "${ROOT_DIR}/scripts/sync_soulx_vendor.sh"
    compose build streaming-video-server model-init
    ;;
  logs)
    compose logs -f streaming-video-server
    ;;
  down)
    compose down
    ;;
  status)
    compose ps
    ;;
  *)
    echo "Usage: $0 [up|init-models|build|logs|down|status]" >&2
    exit 1
    ;;
esac
