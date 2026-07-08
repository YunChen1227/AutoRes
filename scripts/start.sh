#!/usr/bin/env bash
# 启动 AutoRes（裸机部署，Debian 12）。
#
# 注意：SQLite 不是 client-server 数据库，没有独立的"数据库服务"要启动——
# 它就是一个磁盘文件，进程直接打开读写。本脚本第一步做的是「数据库就绪性检查」
# （目录存在、可写、能建表/打开），而不是启动一个数据库进程；确认无误后
# 再依次拉起 scanner（数据管道）与 api（报告服务）两个进程。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$REPO_ROOT/var/run"
LOG_DIR="$REPO_ROOT/var/log"

PYTHON_BIN="${PYTHON_BIN:-python3}"
export PYTHONIOENCODING=utf-8
CONFIG_PATH="${AUTORES_CONFIG:-$REPO_ROOT/config.yaml}"
HEALTH_URL="${AUTORES_HEALTH_URL:-http://127.0.0.1:8080/api/health}"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"   # 30 * 1s = 最多等 30 秒
SCANNER_STARTUP_WAIT="${SCANNER_STARTUP_WAIT:-2}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

# 统一工作目录基准：config.yaml 里的相对路径（如 var/data/autores.db）
# 均相对本仓库根目录解析，检查阶段与实际启动进程阶段必须一致，否则会出现
# "就绪性检查用的文件"和"服务实际用的文件"不是同一个的隐患。
cd "$REPO_ROOT"

# ── 0. 前置检查 ──

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "[start] 错误：未找到 $PYTHON_BIN，请先运行 scripts/install.sh" >&2
    exit 1
fi

if [ ! -f "$CONFIG_PATH" ]; then
    echo "[start] 未发现 $CONFIG_PATH，从 config.example.yaml 生成默认配置..."
    cp "$REPO_ROOT/config.example.yaml" "$CONFIG_PATH"
    echo "[start] 已生成 $CONFIG_PATH，请编辑其中的 llm.base_url / scanner.benchmark_root 后重新运行。"
fi

export AUTORES_CONFIG="$CONFIG_PATH"

if pgrep -f "autores.scanner.main" >/dev/null 2>&1 || pgrep -f "autores.server.main:app" >/dev/null 2>&1; then
    echo "[start] 检测到服务已在运行，如需重启请先执行 scripts/stop.sh" >&2
    exit 1
fi

# ── 1. 数据库文件就绪性检查（不是"启动数据库"，SQLite 无独立进程）──

DB_PATH="$("$PYTHON_BIN" -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from autores.config import load_config
print(load_config('$CONFIG_PATH').database.path)
")"

echo "[start] 数据库文件: $DB_PATH"
DB_DIR="$(dirname "$DB_PATH")"
mkdir -p "$DB_DIR"

if [ ! -w "$DB_DIR" ]; then
    echo "[start] 错误：数据库目录不可写: $DB_DIR" >&2
    exit 1
fi

if ! "$PYTHON_BIN" -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from autores.config import load_config
from autores.db import client as dbc
cfg = load_config('$CONFIG_PATH')
db = dbc.connect(cfg.database)
db.ping()
print('[start] 数据库就绪性检查通过（建表/连接成功）: ' + cfg.database.path)
db.close()
"; then
    echo "[start] 错误：数据库就绪性检查失败，请检查路径与权限" >&2
    exit 1
fi

REPORT_DIR="$("$PYTHON_BIN" -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from autores.config import load_config
print(load_config('$CONFIG_PATH').report.output_dir)
")"
mkdir -p "$REPORT_DIR"
echo "[start] 报告目录就绪: $REPORT_DIR"

# ── 2. 启动 Scanner（数据管道，后台进程）──

echo "[start] 启动 Scanner..."
nohup "$PYTHON_BIN" -m autores.scanner.main >>"$LOG_DIR/scanner.log" 2>&1 &
SCANNER_PID=$!
echo "$SCANNER_PID" > "$RUN_DIR/scanner.pid"
sleep "$SCANNER_STARTUP_WAIT"

if ! kill -0 "$SCANNER_PID" 2>/dev/null; then
    echo "[start] 错误：Scanner 启动后立即退出，查看日志: $LOG_DIR/scanner.log" >&2
    tail -n 30 "$LOG_DIR/scanner.log" >&2 || true
    exit 1
fi
echo "[start] Scanner 已启动 (PID $SCANNER_PID)"

# ── 3. 启动 API（报告服务，后台进程）──

SERVER_HOST="$("$PYTHON_BIN" -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from autores.config import load_config
c = load_config('$CONFIG_PATH').server
print(f'{c.host}:{c.port}')
")"
echo "[start] 启动 API ($SERVER_HOST)..."
nohup "$PYTHON_BIN" -m uvicorn autores.server.main:app \
    --host "${SERVER_HOST%%:*}" --port "${SERVER_HOST##*:}" \
    >>"$LOG_DIR/api.log" 2>&1 &
API_PID=$!
echo "$API_PID" > "$RUN_DIR/api.pid"

if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "[start] 错误：API 启动后立即退出，查看日志: $LOG_DIR/api.log" >&2
    tail -n 30 "$LOG_DIR/api.log" >&2 || true
    exit 1
fi

# ── 4. 健康检查：轮询 /api/health 确认 API 真正就绪 ──

echo "[start] 等待 API 就绪 ($HEALTH_URL)..."
ready=false
for i in $(seq 1 "$HEALTH_RETRIES"); do
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS "$HEALTH_URL" >/tmp/autores_health_check.json 2>/dev/null; then
            ready=true
            break
        fi
    else
        if "$PYTHON_BIN" -c "
import urllib.request, sys
try:
    urllib.request.urlopen('$HEALTH_URL', timeout=2)
    sys.exit(0)
except Exception:
    sys.exit(1)
"; then
            ready=true
            break
        fi
    fi
    sleep 1
done

if [ "$ready" != "true" ]; then
    echo "[start] 错误：API 在 ${HEALTH_RETRIES}s 内未就绪，查看日志: $LOG_DIR/api.log" >&2
    kill "$API_PID" "$SCANNER_PID" 2>/dev/null || true
    exit 1
fi

echo "[start] 健康检查通过："
cat /tmp/autores_health_check.json 2>/dev/null || true
echo
echo "[start] 全部服务已启动。Scanner PID=$SCANNER_PID  API PID=$API_PID"
echo "[start] 日志目录: $LOG_DIR"
echo "[start] 前端访问: http://<服务器IP>:${SERVER_HOST##*:}/"
echo "[start] 停止服务请运行: scripts/stop.sh"
