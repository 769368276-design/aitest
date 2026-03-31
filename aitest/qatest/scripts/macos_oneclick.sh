#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${QA_PORT:-8003}"
INSTALL_BROWSER=1
INSTALL_ONLY=0
USE_WHEELHOUSE=0

if [ -n "${QA_SKIP_PLAYWRIGHT:-}" ]; then INSTALL_BROWSER=0; fi
if [ -n "${QA_INSTALL_ONLY:-}" ]; then INSTALL_ONLY=1; fi
if [ -d "wheelhouse" ] && [ -z "${QA_NO_WHEELHOUSE:-}" ]; then USE_WHEELHOUSE=1; fi

say() { echo "[qatest] $*"; }

say "Project dir: $(pwd)"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python not found. Install Python 3.12+ first." >&2
  exit 1
fi

say "Python: $($PY --version)"

if [ ! -d ".venv" ]; then
  say "Creating venv .venv ..."
  $PY -m venv .venv
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Venv failed: missing .venv/bin/python" >&2
  exit 1
fi

say "Upgrading pip ..."
PIP_ARGS=(--disable-pip-version-check --no-input)
if [ -z "${QA_ALLOW_PIP_CONFIG:-}" ]; then PIP_ARGS+=(--isolated); fi
if [ -n "${QA_UPGRADE_PIP:-}" ]; then
  set +e
  ./.venv/bin/python -m pip install "${PIP_ARGS[@]}" -U pip
  set -e
else
  say "Skipping pip upgrade (set QA_UPGRADE_PIP=1 to enable)."
fi

say "Installing requirements.txt ..."
if [ "${USE_WHEELHOUSE}" = "1" ]; then
  ./.venv/bin/python -m pip install "${PIP_ARGS[@]}" --no-index --find-links wheelhouse -r requirements.txt
else
  ./.venv/bin/python -m pip install "${PIP_ARGS[@]}" -r requirements.txt
fi

ENV_TEMPLATE=""
if [ -f ".env.local.example" ]; then
  ENV_TEMPLATE=".env.local.example"
elif [ -f ".env.example" ]; then
  ENV_TEMPLATE=".env.example"
fi
if [ ! -f ".env" ] && [ -n "${ENV_TEMPLATE}" ]; then
  say "No .env found. Copying ${ENV_TEMPLATE} -> .env"
  cp "${ENV_TEMPLATE}" .env
fi

if [ "${INSTALL_BROWSER}" = "1" ]; then
  if [ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
    export PLAYWRIGHT_BROWSERS_PATH="$(pwd)/.playwright"
  fi
  say "Installing Playwright browser (chromium) ..."
  ./.venv/bin/python -m playwright install chromium

  say "Playwright self-check (launch chromium headless) ..."
  ./.venv/bin/python -c "from playwright.sync_api import sync_playwright;p=sync_playwright().start();b=p.chromium.launch(headless=True);b.close();p.stop();print('ok')"
else
  say "Skipping Playwright install (QA_SKIP_PLAYWRIGHT=1). AI Test requires browser and will fail."
fi

say "Running migrate ..."
./.venv/bin/python manage.py migrate

if [ -z "${INIT_ADMIN_PASSWORD:-}" ]; then
  INIT_ADMIN_PASSWORD="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)"
  export INIT_ADMIN_PASSWORD
  say "Generated INIT_ADMIN_PASSWORD=$INIT_ADMIN_PASSWORD"
fi

say "Running init_data ..."
./.venv/bin/python manage.py init_data

say "Starting server: http://localhost:${PORT}/"
say "After login: set AI keys in Personal Center (AI Generate/AI Test require keys)."
say "Chrome 录制插件(QA Recorder)目录: ./qa_recorder_extension (安装方式见《常见问题处理方案.md》第6节)"
if [ "${INSTALL_ONLY}" = "1" ]; then
  say "QA_INSTALL_ONLY=1, install+init only, not starting server."
  say "Manual start: ./.venv/bin/python manage.py runserver 0.0.0.0:${PORT}"
  exit 0
fi

./.venv/bin/python manage.py runserver 0.0.0.0:${PORT}
