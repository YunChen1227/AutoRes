#!/usr/bin/env bash
# 查看 AutoRes 各进程运行状态与 API 健康检查。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$REPO_ROOT/var/run"
HEALTH_URL="${AUTORES_HEALTH_URL:-http://127.0.0.1:8080/api/health}"

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
