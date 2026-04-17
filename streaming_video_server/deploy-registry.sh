#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${STREAM_ENV_FILE:-${ROOT_DIR}/.env.registry}"
ACTION="${1:-up}"
DOCKER_PREFIX=""

source "${ROOT_DIR}/scripts/docker_common.sh"

compose() {
  docker_cmd compose --env-file "${ENV_FILE}" -f "${ROOT_DIR}/docker-compose.yml" "$@"
}

ensure_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${ROOT_DIR}/.env.registry.example" "${ENV_FILE}"
    echo "Created ${ENV_FILE}. Set IMAGE_NAME before deploying if needed."
  fi
}

validate_image_name() {
  local image_name
  image_name="$(awk -F= '$1=="IMAGE_NAME"{print $2}' "${ENV_FILE}" | tail -n1)"
  if [[ -z "${image_name}" ]]; then
    echo "IMAGE_NAME is not set in ${ENV_FILE}" >&2
    exit 1
  fi
}

check_runtime() {
  check_linux_host
  check_docker_access
  check_compose_access
  check_nvidia_runtime
}

ensure_env_file
validate_image_name

case "${ACTION}" in
  up)
    check_runtime
    compose pull streaming-video-server model-init
    compose run --rm model-init
    compose up -d streaming-video-server
    wait_for_health "${ENV_FILE}"
    compose ps
    ;;
  pull)
    check_runtime
    compose pull streaming-video-server model-init
    ;;
  init-models)
    check_runtime
    compose pull model-init
    compose run --rm model-init
    ;;
  logs)
    check_runtime
    compose logs -f streaming-video-server
    ;;
  down)
    check_runtime
    compose down
    ;;
  status)
    check_runtime
    compose ps
    ;;
  *)
    echo "Usage: $0 [up|pull|init-models|logs|down|status]" >&2
    exit 1
    ;;
esac
