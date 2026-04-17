#!/usr/bin/env bash

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}


docker_cmd() {
  if [[ -n "${DOCKER_PREFIX:-}" ]]; then
    # shellcheck disable=SC2086
    ${DOCKER_PREFIX} docker "$@"
  else
    docker "$@"
  fi
}


resolve_docker_access() {
  if docker info >/dev/null 2>&1; then
    DOCKER_PREFIX=""
    return 0
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Docker is installed, but the current user cannot access /var/run/docker.sock and sudo is unavailable.
Run one of the following before retrying:
  sudo usermod -aG docker "$USER"
  newgrp docker
or run the deployment as root.
EOF
    exit 1
  fi

  export DOCKER_CONFIG="${DOCKER_CONFIG:-${HOME}/.docker}"
  echo "Docker requires elevated access on this host. Docker commands will run via sudo with DOCKER_CONFIG=${DOCKER_CONFIG}." >&2
  DOCKER_PREFIX="sudo --preserve-env=DOCKER_CONFIG"
}


check_docker_access() {
  require_cmd docker
  resolve_docker_access

  if ! docker_cmd info >/dev/null 2>&1; then
    echo "Docker daemon is not reachable." >&2
    exit 1
  fi
}


check_linux_host() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "This Docker stack currently expects Linux host networking." >&2
    exit 1
  fi
}


check_compose_access() {
  if ! docker_cmd compose version >/dev/null 2>&1; then
    echo "Docker Compose v2 is required." >&2
    exit 1
  fi
}


check_nvidia_runtime() {
  if ! docker_cmd info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
    echo "NVIDIA container runtime not detected. Install nvidia-container-toolkit first." >&2
    exit 1
  fi
}


wait_for_health() {
  local env_file="$1"
  local port
  port="$(awk -F= '$1=="STREAM_PORT"{print $2}' "${env_file}" | tail -n1)"
  port="${port:-8080}"

  if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found; skipping health check."
    return 0
  fi

  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
      echo "Service is healthy on http://127.0.0.1:${port}"
      return 0
    fi
    sleep 2
  done

  echo "Service did not become healthy within the expected time." >&2
  return 1
}
