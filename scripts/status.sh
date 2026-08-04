#!/usr/bin/env bash
# 查看 AutoRes 各进程运行状态与 API 健康检查。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$REPO_ROOT/var/run"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CONFIG_PATH="${AUTORES_CONFIG:-$REPO_ROOT/config.yaml}"

# 与 start.sh 一致：默认跟 config.yaml 的 server.port，可用 AUTORES_HEALTH_URL 覆盖
if [ -n "${AUTORES_HEALTH_URL:-}" ]; then
    HEALTH_URL="$AUTORES_HEALTH_URL"
else
    SERVER_PORT="$("$PYTHON_BIN" -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from autores.config import load_config
print(load_config('$CONFIG_PATH').server.port)
" 2>/dev/null || echo 8080)"
    HEALTH_URL="http://127.0.0.1:${SERVER_PORT}/api/health"
fi

check_one() {
    local name="$1" pidfile="$2"
    if [ ! -f "$pidfile" ]; then
        echo "[status] $name: 未启动（无 PID 文件）"
        return
    fi
    local pid
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
        echo "[status] $name: 运行中 (PID $pid)"
    else
        echo "[status] $name: PID 文件存在但进程已退出 (PID $pid)"
    fi
}

check_one "Scanner" "$RUN_DIR/scanner.pid"
check_one "API" "$RUN_DIR/api.pid"

echo "[status] API 健康检查 ($HEALTH_URL):"
if command -v curl >/dev/null 2>&1; then
    curl -fsS "$HEALTH_URL" || echo "  不可达"
else
    python3 -c "
import urllib.request
try:
    print(urllib.request.urlopen('$HEALTH_URL', timeout=2).read().decode())
except Exception as e:
    print('  不可达:', e)
"
fi
echo
