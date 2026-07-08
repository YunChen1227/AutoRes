#!/usr/bin/env bash
# 停止 AutoRes 的 scanner / api 进程（按 start.sh 记录的 PID 文件）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$REPO_ROOT/var/run"

stop_one() {
    local name="$1" pidfile="$2"
    if [ ! -f "$pidfile" ]; then
        echo "[stop] $name: 无 PID 文件，跳过"
        return
    fi
    local pid
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid"
        echo "[stop] $name (PID $pid) 已发送停止信号"
    else
        echo "[stop] $name (PID $pid) 已不在运行"
    fi
    rm -f "$pidfile"
}

stop_one "API" "$RUN_DIR/api.pid"
stop_one "Scanner" "$RUN_DIR/scanner.pid"

echo "[stop] 完成。"
