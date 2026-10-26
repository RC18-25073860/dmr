#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
DMR_PYTHON_BIN="${DMR_PYTHON_BIN:-python3}"
"$DMR_PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "需要 Python 3.10 或以上；可用 DMR_PYTHON_BIN 指定解释器"'
if [ ! -x "$DMR_PROJECT_ROOT/.venv/bin/python" ]; then
  "$DMR_PYTHON_BIN" -m venv "$DMR_PROJECT_ROOT/.venv"
fi
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip check
cd frontend
npm ci
npm run build
printf '\n安装和构建完成。运行：./启动本地DMR.command\n'
