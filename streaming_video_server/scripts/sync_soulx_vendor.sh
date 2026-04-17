#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${1:-${SOULX_SOURCE_DIR:-${ROOT_DIR}/../SoulX-FlashHead}}"
TARGET_DIR="${ROOT_DIR}/vendor/SoulX-FlashHead"

mkdir -p "${TARGET_DIR}"

if [[ ! -d "${SOURCE_DIR}" ]]; then
  if [[ -f "${TARGET_DIR}/requirements.txt" ]]; then
    echo "SoulX source directory not found: ${SOURCE_DIR}"
    echo "Using existing vendored SoulX-FlashHead source in ${TARGET_DIR}"
    exit 0
  fi

  echo "SoulX source directory not found: ${SOURCE_DIR}" >&2
  echo "No vendored SoulX-FlashHead source found in ${TARGET_DIR}" >&2
  exit 1
fi

rsync -a --delete \
  --exclude ".git" \
  --exclude ".DS_Store" \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude "models" \
  "${SOURCE_DIR}/" "${TARGET_DIR}/"

echo "Vendored SoulX-FlashHead source into ${TARGET_DIR}"
