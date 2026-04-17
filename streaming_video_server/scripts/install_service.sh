#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="streaming-video-server.service"
SERVICE_FILE="${ROOT_DIR}/deploy/systemd/${SERVICE_NAME}"

cat <<EOF
Install steps:
1. sudo cp "${SERVICE_FILE}" /etc/systemd/system/${SERVICE_NAME}
2. sudo systemctl daemon-reload
3. sudo systemctl enable --now ${SERVICE_NAME}
4. sudo systemctl status ${SERVICE_NAME}
EOF
