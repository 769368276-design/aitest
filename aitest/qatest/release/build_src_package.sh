#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${PROJECT_ROOT}/dist"
mkdir -p "${OUT_DIR}"

DATE="$(date +%Y%m%d-%H%M%S)"
ZIP_PATH="${OUT_DIR}/qatest-src-${DATE}.zip"
TMP_DIR="${OUT_DIR}/_src_tmp_${DATE}"

cleanup() { rm -rf "${TMP_DIR}" || true; }
trap cleanup EXIT

mkdir -p "${TMP_DIR}"

rsync -a --delete \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude "dist" \
  --exclude "backup" \
  --exclude "media" \
  --exclude "staticfiles" \
  --exclude ".pytest_cache" \
  --exclude ".mypy_cache" \
  --exclude "wheelhouse" \
  --exclude ".playwright" \
  --exclude "_pkg_smoketest" \
  "${PROJECT_ROOT}/" "${TMP_DIR}/"

find "${TMP_DIR}" -type d -name "__pycache__" -print0 | xargs -0 rm -rf || true
find "${TMP_DIR}" -type f \( -name "*.pyc" -o -name "*.pyo" -o -name ".DS_Store" -o -name ".env" -o -name "db.sqlite3" -o -name "*.zip" -o -name "*.tar" -o -name "*.tgz" -o -name "*.gz" \) -print0 | xargs -0 rm -f || true

rm -f "${ZIP_PATH}" || true
(cd "${TMP_DIR}" && /usr/bin/zip -r "${ZIP_PATH}" . >/dev/null)

echo "Done: ${ZIP_PATH}"

