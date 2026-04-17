#!/usr/bin/env bash
set -euo pipefail

CKPT_DIR="${SOULX_CKPT_DIR:-/models/SoulX-FlashHead-1_3B}"
WAV2VEC_DIR="${SOULX_WAV2VEC_DIR:-/models/wav2vec2-base-960h}"
MODEL_DOWNLOAD_FORCE="${MODEL_DOWNLOAD_FORCE:-0}"
HF_BIN="${HF_BIN:-hf}"

mkdir -p "${CKPT_DIR}" "${WAV2VEC_DIR}"

download_repo() {
  local repo_id="$1"
  local target_dir="$2"
  local marker_file="$3"

  if [[ "${MODEL_DOWNLOAD_FORCE}" != "1" && -e "${target_dir}/${marker_file}" ]]; then
    echo "Skipping ${repo_id}; found ${target_dir}/${marker_file}"
    return 0
  fi

  echo "Downloading ${repo_id} into ${target_dir}"
  "${HF_BIN}" download "${repo_id}" --local-dir "${target_dir}"
}

download_repo "Soul-AILab/SoulX-FlashHead-1_3B" "${CKPT_DIR}" "model_index.json"
download_repo "facebook/wav2vec2-base-960h" "${WAV2VEC_DIR}" "config.json"

echo "Model bootstrap complete."

