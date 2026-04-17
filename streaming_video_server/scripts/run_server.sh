#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${STREAM_PYTHON_BIN:-}" ]]; then
  PYTHON_BIN="${STREAM_PYTHON_BIN}"
else
  PYTHON_BIN=""
  for candidate in \
    "/opt/venv/bin/python"
  do
    if [[ -x "${candidate}" ]]; then
      PYTHON_BIN="${candidate}"
      break
    fi
  done

  if [[ -z "${PYTHON_BIN}" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v python)"
    else
      echo "No Python interpreter found. Set STREAM_PYTHON_BIN explicitly." >&2
      exit 1
    fi
  fi
fi

if [[ -f "${ROOT_DIR}/.env.server" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env.server"
  set +a
fi

cd "${ROOT_DIR}"
exec "${PYTHON_BIN}" -m server.app
