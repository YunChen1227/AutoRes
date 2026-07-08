#!/usr/bin/env bash
# 安装运行时依赖到系统 Python（不用虚拟环境，Debian 12 专用服务器场景）。
# Debian 12 的 pip 默认拒绝直接装到系统环境（PEP 668），加 --break-system-packages 绕过。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "[install] 使用解释器: $($PYTHON_BIN --version 2>&1) ($(command -v "$PYTHON_BIN"))"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "[install] 错误：未找到 $PYTHON_BIN，请先安装 Python 3.11（apt install python3 python3-pip）" >&2
    exit 1
fi

echo "[install] 安装 pip（如缺失）..."
"$PYTHON_BIN" -m ensurepip --upgrade >/dev/null 2>&1 || true

echo "[install] 安装依赖: $REPO_ROOT/requirements.txt"
"$PYTHON_BIN" -m pip install --break-system-packages --no-input \
    -r "$REPO_ROOT/requirements.txt"

echo "[install] 完成。"
