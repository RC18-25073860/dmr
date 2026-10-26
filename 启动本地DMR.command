#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/scripts/env.sh"
export DMR_DATA_DIR="${DMR_DATA_DIR:-$DMR_PROJECT_ROOT/data}"
export DMR_AUTH_MODE="${DMR_AUTH_MODE:-local}"
if [ ! -x "$DMR_PROJECT_ROOT/.venv/bin/python" ]; then
  printf '缺少项目虚拟环境，请先按本地运行说明执行 scripts/setup.sh。\n' >&2
  exit 1
fi
if [ ! -f "$DMR_PROJECT_ROOT/frontend/dist/index.html" ]; then
  printf '缺少前端构建，请在项目中执行 scripts/build.sh。\n' >&2
  exit 1
fi
printf '本地访问：http://127.0.0.1:%s/\n' "${DMR_PORT:-8920}"
printf '关闭服务：在此窗口按 Ctrl+C。数据目录：%s\n' "$DMR_DATA_DIR"
"$DMR_PROJECT_ROOT/.venv/bin/python" -u "$DMR_PROJECT_ROOT/server.py" --port "${DMR_PORT:-8920}" 2>&1 | tee -a "$DMR_PROJECT_ROOT/logs/server.log"
