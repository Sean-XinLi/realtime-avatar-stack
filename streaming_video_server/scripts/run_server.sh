#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${STREAM_PYTHON_BIN:-python3}"

if [[ -f "${ROOT_DIR}/.env.server" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env.server"
  set +a
fi

cd "${ROOT_DIR}"
exec "${PYTHON_BIN}" -m server.app
