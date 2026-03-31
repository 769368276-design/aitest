#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
echo "=========================================="
echo "qatest 一键安装 & 启动 (macOS)"
echo "=========================================="
echo ""
echo "将执行：创建 .venv -> 安装依赖 -> 安装 Playwright 浏览器 -> migrate/init_data -> 启动服务"
echo ""
chmod +x "./scripts/macos_oneclick.sh" || true
./scripts/macos_oneclick.sh
