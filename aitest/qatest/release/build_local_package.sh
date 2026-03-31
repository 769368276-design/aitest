#!/bin/bash
set -euo pipefail

BUILD_WHEELHOUSE="${BUILD_WHEELHOUSE:-1}"
INCLUDE_PLAYWRIGHT_BROWSERS="${INCLUDE_PLAYWRIGHT_BROWSERS:-0}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${PROJECT_ROOT}/dist"
mkdir -p "${OUT_DIR}"

DATE="$(date +%Y%m%d_%H%M%S)"
ZIP_PATH="${OUT_DIR}/qatest_local_clean_macos_${DATE}.zip"
TMP_DIR="${OUT_DIR}/_local_pkg_tmp_${DATE}"

cleanup() { rm -rf "${TMP_DIR}" || true; }
trap cleanup EXIT

mkdir -p "${TMP_DIR}"

rsync -a --delete \
  --exclude ".venv" \
  --exclude ".git" \
  --exclude "__pycache__" \
  --exclude ".pytest_cache" \
  --exclude ".mypy_cache" \
  --exclude "backup" \
  --exclude "dist" \
  --exclude "packages" \
  --exclude "deploy" \
  --exclude "_old" \
  --exclude "media" \
  --exclude "staticfiles" \
  --exclude ".github" \
  --exclude "_pkg_smoketest" \
  "${PROJECT_ROOT}/" "${TMP_DIR}/"

find "${TMP_DIR}" -type f \( \
  -name "deploy.ps1" -o -name "deploy.cmd" -o -name "Dockerfile" -o \
  -name ".dockerignore" -o \
  -name "docker-compose.prod.yml" -o -name "docker-compose.yml" -o -name "docker-compose.*.yml" -o \
  -name "部署指南傻瓜版.md" -o -name "部署指南傻瓜版-macOS.md" \
  \) -print0 | xargs -0 rm -f || true

find "${TMP_DIR}" -type d -name "__pycache__" -print0 | xargs -0 rm -rf || true
find "${TMP_DIR}" -type f \( -name "*.pyc" -o -name "*.pyo" -o -name ".DS_Store" -o -name ".env" -o -name "db.sqlite3" -o -name "*.zip" -o -name "*.tar" -o -name "*.tgz" -o -name "*.gz" \) -print0 | xargs -0 rm -f || true

if [ "${INCLUDE_PLAYWRIGHT_BROWSERS}" = "1" ] && [ -d "${PROJECT_ROOT}/.playwright" ]; then
  rsync -a "${PROJECT_ROOT}/.playwright/" "${TMP_DIR}/.playwright/"
fi

if [ "${BUILD_WHEELHOUSE}" = "1" ]; then
  if command -v python3 >/dev/null 2>&1; then PY=python3; elif command -v python >/dev/null 2>&1; then PY=python; else
    echo "Python not found. Install Python 3.12+ before building package." >&2
    exit 1
  fi

  mkdir -p "${TMP_DIR}/wheelhouse"
  export PIP_DISABLE_PIP_VERSION_CHECK=1
  export PIP_NO_INPUT=1

  set +e
  "${PY}" -m pip download --only-binary=:all: -d "${TMP_DIR}/wheelhouse" -r "${TMP_DIR}/requirements.txt"
  RC=$?
  set -e
  if [ "${RC}" != "0" ]; then
    echo "Warning: wheel-only download failed, fallback to allowing sdists."
    "${PY}" -m pip download -d "${TMP_DIR}/wheelhouse" -r "${TMP_DIR}/requirements.txt"
  fi
fi

rm -f "${ZIP_PATH}" || true
(cd "${TMP_DIR}" && /usr/bin/zip -r "${ZIP_PATH}" . >/dev/null)

echo "完成：${ZIP_PATH}"
