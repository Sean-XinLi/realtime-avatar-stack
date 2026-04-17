#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="/opt/streaming-video-server"
DEFAULT_AVATAR_DIR="${ROOT_DIR}/assets/avatars"
USER_AVATAR_DIR="/data/avatars"
MODEL_AUTO_DOWNLOAD="${MODEL_AUTO_DOWNLOAD:-1}"

if [[ -f "${ROOT_DIR}/.env.server" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env.server"
  set +a
fi

if [[ ! -d "${SOULX_ROOT:-/opt/soulx}" ]]; then
  echo "SoulX source directory not found: ${SOULX_ROOT:-/opt/soulx}" >&2
  exit 1
fi

if [[ -d "${USER_AVATAR_DIR}" ]] && find "${USER_AVATAR_DIR}" -maxdepth 1 -type f \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' \) | grep -q .; then
  export SOULX_COND_IMAGE="${USER_AVATAR_DIR}"
elif [[ -z "${SOULX_COND_IMAGE:-}" || ! -e "${SOULX_COND_IMAGE}" ]]; then
  export SOULX_COND_IMAGE="${DEFAULT_AVATAR_DIR}"
fi

missing_models=0
if [[ ! -f "${SOULX_CKPT_DIR:-/models/SoulX-FlashHead-1_3B}/model_index.json" ]]; then
  missing_models=1
fi
if [[ ! -f "${SOULX_WAV2VEC_DIR:-/models/wav2vec2-base-960h}/config.json" ]]; then
  missing_models=1
fi

if [[ "${missing_models}" == "1" ]]; then
  if [[ "${MODEL_AUTO_DOWNLOAD}" == "0" ]]; then
    echo "Model checkpoint is incomplete and MODEL_AUTO_DOWNLOAD=0." >&2
    echo "Run scripts/bootstrap_models.sh or docker compose run --rm model-init first." >&2
    exit 1
  fi

  echo "Model files are missing. Bootstrapping them now..." >&2
  "${ROOT_DIR}/scripts/bootstrap_models.sh"
fi

cd "${ROOT_DIR}"
exec python -m server.app
