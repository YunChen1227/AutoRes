#!/bin/bash
# ============================================================================
# 压测脚本：sglang / vllm 通用，额外尝试抓取
#   1) KV cache hit rate（两框架强制对齐，跨框架可比，统一 0-100 百分比）
#   2) spec decoding 接受率/接受长度（两框架颗粒度不同，不对齐，跨框架不可比）
#   3) warmup 预热（两框架强制对齐：bench 原生 warmup 一律关掉，改由脚本自己发）
#
# ★ 两个 framework 是分开的，一共 4 种组合：
#     SERVER_FRAMEWORK  推理服务本身的框架  → 决定 flush 端点、/metrics、/server_info
#     BENCH_FRAMEWORK   压测工具的框架       → 决定跑哪个 bench、产出 JSON 的字段结构
#   （sglang bench 能打 vllm server，vllm bench 也能打 sglang server，故二者可不同）
#
# ★ 关键：指标"来源"看 SERVER，指标"落到 JSON 的 key"看 BENCH（因为 to_csv 按
#   bench 框架解析 JSON）。to_csv 的 --framework 必须传 BENCH_FRAMEWORK！
#
# ────────────────────────── 4 种组合行为矩阵 ──────────────────────────
# server | bench  | flush端点          | KV hit rate                       | spec
# -------|--------|--------------------|-----------------------------------|--------------------
# sglang | sglang | /flush_cache       | bench --cache-report(原生,精确)    | 原生 accept_length
# sglang | vllm   | /flush_cache       | N/A(sglang只有瞬时gauge,不硬凑)    | N/A(颗粒度错位)
# vllm   | sglang | /reset_prefix_cache| 抓 vllm 计数器delta→注入嵌套cache  | N/A(颗粒度错位)
# vllm   | vllm   | /reset_prefix_cache| 抓 vllm 计数器delta→注入kv_cache   | 原生 spec_decode_*
# ----------------------------------------------------------------------
#   · KV：server=vllm 用 /metrics 计数器 delta（精确，与 bench 无关）；
#         server=sglang 只能靠 sglang bench 的 --cache-report，换 vllm bench 就拿不到。
#   · spec：只在 server==bench（原生路径）时采集；跨框架会把 accept_length 当成
#         accept_rate 之类误贴标签，故直接 N/A（"实在没有也不强求"）。
#   · 抓不到的指标一律留空 / N/A，绝不阻断压测。
# ============================================================================

# ┌──────────────────────── 配置区（改这里，不再传参）────────────────────────┐
SERVER_FRAMEWORK="sglang"     # 推理服务框架： sglang | vllm
BENCH_FRAMEWORK="sglang"      # 压测工具框架： sglang | vllm

# ── 部署模式：colocated=单机/分布式；pd_disagg=PD 分离 ──
#   PD 分离时 bench / flush / KV 均只打单一入口（router 或 proxy 的 base url），
#   与 sglang/vllm 官方 bench 一致；PREFILL_CMD/DECODE_CMD 仅用于 to_csv 落盘解析。
DEPLOYMENT_MODE="colocated"   # colocated | pd_disagg
# PD 分离必填（各是一条完整 server 启动命令，需含角色标识）：
#   sglang: --disaggregation-mode prefill|decode
#   vllm  : --kv-transfer-config '{"kv_role":"kv_producer|kv_consumer", ...}'
PREFILL_CMD=""
DECODE_CMD=""
ROUTER_CMD=""                 # 可选：router/proxy 命令（--policy/--prefill-policy/--decode-policy）

# ── 压测入口（单一 base url，与 sglang/vllm bench 一致）──
#   host_port —— 等价于 http://SERVER_HOST:SERVER_PORT（bench 传 --host/--port）
#   url       —— 完整 base url（bench 传 --base-url；PD 填 router/proxy 地址）
ENDPOINT_MODE="host_port"    # host_port | url
SERVER_HOST="30.205.160.45"
SERVER_PORT="18000"
SERVER_URL=""                # url 模式示例：https://gateway.example.com/v1/sglang

# Token 认证（可选）：sglang/vllm bench 读 OPENAI_API_KEY → Authorization: Bearer …
# 脚本会在压测 / flush / metrics 抓取前 export；留空表示无鉴权
API_KEY=""

MODEL="deepseek_v4"
TOKENIZER="/mnt/pvc/pvc-sfe-platform-id10001749-vol633083-prd/llm_model/DeepSeek-V4-Flash-w8a8-mtp"

OUTPUT_LEN=1024
FLUSH_CACHE=0                 # 1=每轮压测前清 server 缓存（会把 KV hit rate 压到冷启动）

# ── 预热请求数（两框架强制对齐，跨框架可比）──
#   0 = 不预热。两个 bench 的原生 warmup 都显式关掉：vllm --num-warmups 原生默认就是 0，
#       sglang --warmup-requests 原生默认是 1，不显式传 0 会偷偷多打一条。
#   >0 = 由脚本自己发 N 条预热请求，bench 侧仍然传 0，以绕开两框架的实现差异：
#       · 原生 warmup 输出长度不同：sglang 截到 32 token，vllm 用完整 OUTPUT_LEN（差 ~32 倍）
#       · 原生 warmup 并发不同：sglang 一次性全发，vllm 受 --max-concurrency 限流
#       · 「预热后清缓存」只有 sglang 有（--flush-cache），vllm bench 没有对应开关
#       · server=vllm 时 KV hit rate 靠 /metrics 计数器 delta，原生 warmup 会混进 delta；
#         而 sglang --cache-report 只统计正式请求，两者口径天然不一致
#   脚本自管预热后，4 种组合的预热强度、清缓存时机、KV 采样基线完全一致。
WARMUP_REQUESTS=0
WARMUP_OUTPUT_LEN=32          # 预热请求输出长度（对齐 sglang 原生 warmup 的 32 token 上限）

# ── 共享前缀比例数组（0~1，行键维度 Prefix_Rate，写入 metrics / _autores_dims）──
#   作为第 3 层循环：concurrency → input_len → prefix_rate。
#   每轮真实前缀长度 = round(INPUT_LEN * PREFIX_RATE)，正文长度 = INPUT_LEN - 前缀，
#   保证两框架的「总输入 = INPUT_LEN」一致，可横向比较。
#   PREFIX_RATE=0 → 无前缀（vllm: --random-prefix-len 0；sglang: 沿用 random-ids）。
#   PREFIX_RATE>0 → vllm 用 random 数据集的 --random-prefix-len；
#                   sglang random 数据集无前缀参数，改用 generated-shared-prefix
#                   （--gsp-num-groups 1 单一全局前缀，逆向映射逼近 vllm，强行可比）。
declare -a PREFIX_RATES=(0.0)

LOG_SUBDIR="logs_910b_cjb_dsv4flashint8_8_260723"
FILE_PREFIX="dsv4_jk_ori"     # 输出文件名前缀

# ── 压测结束后自动落盘（调用 to_csv.py）配置 ──
#   RUN_TO_CSV=1 时，全部压测跑完自动整理成 result.csv + metadata.json 落到 NAS_DIR。
#   flush_cache 直接用上面的 FLUSH_CACHE，无需再传；server/bench 框架用上面两个变量。
RUN_TO_CSV=1
NAS_DIR="/mnt/nas/benchmark_root"          # to_csv 落盘根目录（其下建时间戳目录）
FRAMEWORK_VERSION="0.4.6"                   # server 框架版本 → test_runs.framework_version
GPU_TYPE="910B4-64G"                        # 显卡型号 → test_runs.gpu_type（需在 gpu_memory_presets 内）
LAUNCH_CMD="python -m sglang.launch_server --tp-size 8"   # server 启动命令 → test_runs.launch_cmd
# 以下三项都可留空：填了 MODEL_CONFIG 时，参数量按 config 的形状字段估算，
# 权重占用与权重精度直接推导（量化 checkpoint 会正确识别成 fp8/int4 等）。
MODEL_PARAMS_B=""                           # 参数量，单位 B（7B 模型填 7.62）→ test_runs.model_params_b
MODEL_WEIGHT_GB=""                          # 权重实际占用，单位 GiB → test_runs.model_weight_gb
MODEL_DTYPE=""                              # 权重精度：bf16|fp16|fp8|int8|int4|fp4 → test_runs.model_dtype
# 模型目录下的 config.json 路径。强烈建议填：context_length / dtype / quantization /
# max-num-batched-tokens 这些参数启动命令里通常不写，是 vllm/sglang 读它推导出来的；
# 上面三项元信息也靠它推导。不给则相应列留空（详见 tools/model_config.py）。
MODEL_CONFIG=""                             # 例：/models/GLM-4.5/config.json
# vllm bench 场景 to_csv 需从 bench 命令补 Input_Length（sglang bench 无需）。
# 注意：本脚本每轮 input-len 都不同，单一 --bench-cmd 无法覆盖全部，vllm bench 下
# Input_Length 可能落 N/A —— 这是 to_csv 对 vllm bench 的既有限制，与本次改动无关。
BENCH_CMD=""

# 并发数
declare -a max_concurrency=(8 16 32 64 128 256 512)
# 并发数 → 输入长度列表
declare -A input_length_map=(
    [1]="1024 2048 4096 8192 16384 32768 65538 128000"
    [2]="1024 2048 4096 8192 16384 32768 65538 128000"
    [4]="1024 2048 4096 8192 16384 32768 65538 128000"
    [8]="1024 2048 4096 8192 16384 32768 65538 128000"
    [16]="1024 2048 4096 8192 16384 32768 65538 128000"
    [32]="1024 2048 4096 8192 16384 32768 65538 128000"
    [64]="1024 2048 4096 8192 16384 32768 65538"
    [128]="1024 2048 4096 8192 16384 32768"
    [256]="1024 2048 4096 8192 16384 32768"
    [512]="1024 2048 4096 8192 16384"
)
# └────────────────────────────────────────────────────────────────────────┘

# ── 解析压测入口 & 鉴权（sglang/vllm bench 与 curl admin 共用）──
if [[ "$ENDPOINT_MODE" != "host_port" && "$ENDPOINT_MODE" != "url" ]]; then
    echo "[ERR] ENDPOINT_MODE 只能是 host_port | url，当前=$ENDPOINT_MODE" >&2
    exit 1
fi
if [[ "$ENDPOINT_MODE" == "url" ]]; then
    if [[ -z "$SERVER_URL" ]]; then
        echo "[ERR] ENDPOINT_MODE=url 需配置 SERVER_URL（完整 base url）" >&2
        exit 1
    fi
    BASE_URL="${SERVER_URL%/}"
else
    if [[ -z "$SERVER_HOST" || -z "$SERVER_PORT" ]]; then
        echo "[ERR] ENDPOINT_MODE=host_port 需配置 SERVER_HOST 与 SERVER_PORT" >&2
        exit 1
    fi
    BASE_URL="http://${SERVER_HOST}:${SERVER_PORT}"
fi

CURL_AUTH=()
if [[ -n "$API_KEY" ]]; then
    export OPENAI_API_KEY="$API_KEY"
    export API_KEY="$API_KEY"
    CURL_AUTH=(-H "Authorization: Bearer ${API_KEY}")
fi

# vllm / sglang bench 的 endpoint 参数（host_port 与 url 二选一，禁止同时传）
_vllm_endpoint_args() {
    if [[ "$ENDPOINT_MODE" == "url" ]]; then
        printf '%s\n' --base-url "$BASE_URL"
    else
        printf '%s\n' --host "$SERVER_HOST" --port "$SERVER_PORT"
    fi
}

_sglang_endpoint_args() {
    if [[ "$ENDPOINT_MODE" == "url" ]]; then
        printf '%s\n' --base-url "$BASE_URL"
    else
        printf '%s\n' --host "$SERVER_HOST" --port "$SERVER_PORT"
    fi
}

BASE_LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${LOG_SUBDIR}"
mkdir -p "$BASE_LOG_DIR"

# ── 部署模式校验（PD 分离必须给出 prefill/decode 启动命令，供 to_csv 拆列入库）──
if [[ "$DEPLOYMENT_MODE" != "colocated" && "$DEPLOYMENT_MODE" != "pd_disagg" ]]; then
    echo "[ERR] DEPLOYMENT_MODE 只能是 colocated | pd_disagg，当前=$DEPLOYMENT_MODE" >&2
    exit 1
fi
if [[ "$DEPLOYMENT_MODE" == "pd_disagg" ]]; then
    if [[ -z "$PREFILL_CMD" || -z "$DECODE_CMD" ]]; then
        echo "[ERR] DEPLOYMENT_MODE=pd_disagg 需配置 PREFILL_CMD 与 DECODE_CMD" >&2
        exit 1
    fi
fi

# ── 共享前缀比例数组校验（每个 rate ∈ [0,1)）──
if (( ${#PREFIX_RATES[@]} == 0 )); then
    echo "[ERR] PREFIX_RATES 不能为空" >&2
    exit 1
fi
for _pr in "${PREFIX_RATES[@]}"; do
    if ! awk -v r="$_pr" 'BEGIN{exit !(r+0==r && r>=0 && r<1)}' </dev/null 2>/dev/null; then
        echo "[ERR] PREFIX_RATES 每项必须是 [0,1) 之间的数字，当前=$_pr" >&2
        exit 1
    fi
done

# ── 预热参数校验 ──
if ! [[ "$WARMUP_REQUESTS" =~ ^[0-9]+$ ]]; then
    echo "[ERR] WARMUP_REQUESTS 必须是非负整数，当前=$WARMUP_REQUESTS" >&2
    exit 1
fi
if ! [[ "$WARMUP_OUTPUT_LEN" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERR] WARMUP_OUTPUT_LEN 必须是正整数，当前=$WARMUP_OUTPUT_LEN" >&2
    exit 1
fi

# ── 依据组合预解析各指标的采集策略（只算一次）──
#   CAPTURE_KV  : none | sglang_native | scrape_vllm
#   CAPTURE_SPEC: none | native
if [[ "$SERVER_FRAMEWORK" == "vllm" ]]; then
    CAPTURE_KV="scrape_vllm"
elif [[ "$BENCH_FRAMEWORK" == "sglang" ]]; then
    CAPTURE_KV="sglang_native"     # server=sglang & bench=sglang
else
    CAPTURE_KV="none"              # server=sglang & bench=vllm
fi
if [[ "$SERVER_FRAMEWORK" == "$BENCH_FRAMEWORK" ]]; then
    CAPTURE_SPEC="native"
else
    CAPTURE_SPEC="none"
fi

# sglang bench 打不同 server 用不同 backend
if [[ "$SERVER_FRAMEWORK" == "sglang" ]]; then
    SGLANG_BENCH_BACKEND="sglang-oai-chat"
else
    SGLANG_BENCH_BACKEND="vllm"
fi

# ── 探测 bench 是否支持关闭原生 warmup ──
#   预热统一由脚本负责，bench 侧一律传 0。老版本 bench 不认这个参数，硬传会直接报错退出，
#   故先探一次；探不到就只告警（sglang 会退回原生默认的 1 条预热，两框架不再严格对齐）。
if [[ "$BENCH_FRAMEWORK" == "vllm" ]]; then
    BENCH_WARMUP_FLAG="--num-warmups"
    BENCH_WARMUP_HELP="$(vllm bench serve --help 2>/dev/null)"
else
    BENCH_WARMUP_FLAG="--warmup-requests"
    BENCH_WARMUP_HELP="$(python3 -m sglang.bench_serving --help 2>/dev/null)"
fi
WARMUP_OFF=()
if grep -q -- "$BENCH_WARMUP_FLAG" <<<"$BENCH_WARMUP_HELP"; then
    BENCH_WARMUP_FLAG_OK=1
    WARMUP_OFF=("$BENCH_WARMUP_FLAG" 0)
else
    BENCH_WARMUP_FLAG_OK=0
fi

echo "================= 压测配置 ================="
echo "  SERVER_FRAMEWORK = $SERVER_FRAMEWORK"
echo "  BENCH_FRAMEWORK  = $BENCH_FRAMEWORK"
echo "  DEPLOYMENT_MODE  = $DEPLOYMENT_MODE"
echo "  ENDPOINT_MODE    = $ENDPOINT_MODE"
if [[ "$ENDPOINT_MODE" == "url" ]]; then
    echo "  BASE_URL         = $BASE_URL"
else
    echo "  BASE_URL         = $BASE_URL  (host_port: ${SERVER_HOST}:${SERVER_PORT})"
fi
[[ -n "$API_KEY" ]] && echo "  API_KEY          = (已配置，bench/flush/metrics 带 Bearer)"
echo "  KV 采集策略      = $CAPTURE_KV"
echo "  spec 采集策略    = $CAPTURE_SPEC"
echo "  FLUSH_CACHE      = $FLUSH_CACHE"
if (( WARMUP_REQUESTS > 0 )); then
    echo "  WARMUP_REQUESTS  = $WARMUP_REQUESTS  (脚本自管，输出 ${WARMUP_OUTPUT_LEN} token；bench 原生 warmup 关闭)"
else
    echo "  WARMUP_REQUESTS  = 0  (不预热)"
fi
echo "  PREFIX_RATES     = ${PREFIX_RATES[*]}  (第 3 层循环；>0 时 sglang→GSP / vllm→random-prefix-len)"
echo "  ⚠ 落盘请执行: to_csv.py --benchmark-kind text --framework $BENCH_FRAMEWORK ..."
if [[ "$BENCH_WARMUP_FLAG_OK" == "0" ]]; then
    if [[ "$BENCH_FRAMEWORK" == "sglang" ]]; then
        echo "  ⚠ 当前 sglang bench 不认 $BENCH_WARMUP_FLAG，原生那 1 条预热关不掉 → 实际预热=${WARMUP_REQUESTS}+1，与 vllm 不对齐"
    else
        echo "  ⚠ 当前 vllm bench 不认 $BENCH_WARMUP_FLAG（老版本无此参数，原生默认即 0 预热，不影响对齐）"
    fi
fi
[[ "$CAPTURE_KV" == "none" ]] && echo "  ⚠ 该组合无法可靠获取 KV hit rate（sglang server 请改用 sglang bench + --cache-report）"
[[ "$CAPTURE_SPEC" == "none" && ( "$SERVER_FRAMEWORK" != "$BENCH_FRAMEWORK" ) ]] && echo "  ⚠ server≠bench：spec 指标颗粒度错位，本轮不采集（避免误贴标签）"
echo "============================================"

# ---- 工具函数（flush / metrics 均只打 BASE_URL，与官方 bench 一致）----

# 清空 server 缓存（sglang: /flush_cache；vllm: /reset_prefix_cache）
flush_server_cache() {
    [[ "$FLUSH_CACHE" == "1" ]] || return 0
    if [[ "$SERVER_FRAMEWORK" == "vllm" ]]; then
        curl -s "${CURL_AUTH[@]}" -X POST "${BASE_URL}/reset_prefix_cache" >/dev/null 2>&1 \
            || echo "[WARN] reset_prefix_cache 失败（vllm server 需 VLLM_SERVER_DEV_MODE=1）"
    else
        curl -s "${CURL_AUTH[@]}" -X POST "${BASE_URL}/flush_cache?timeout=60" >/dev/null 2>&1 \
            || echo "[WARN] flush_cache 失败"
    fi
}

# 预热：脚本自己发 WARMUP_REQUESTS 条请求，两框架走同一条 OpenAI 兼容路径
# (/v1/chat/completions)，保证 4 种组合的预热强度一致。预热完再清一次缓存（受
# FLUSH_CACHE 控制），之后调用方才去采 KV 基线，故预热流量不会混进 KV hit rate。
#   $1=本轮输入长度  $2=本轮并发
run_warmup() {
    local input_len="$1" conc="$2"
    (( WARMUP_REQUESTS > 0 )) || return 0
    echo "[warmup] ${WARMUP_REQUESTS} 条（input≈${input_len} token，max_tokens=${WARMUP_OUTPUT_LEN}，并发≤${conc}）"
    python3 - "$BASE_URL" "$MODEL" "$input_len" "$WARMUP_OUTPUT_LEN" \
             "$WARMUP_REQUESTS" "$conc" "${API_KEY}" <<'PYEOF'
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

base_url, model, input_len, out_len, n, conc, api_key = sys.argv[1:8]
input_len, out_len, n, conc = int(input_len), int(out_len), int(n), int(conc)

# 近似 input_len 个 token 的填充文本：预热只为打热 kernel / CUDA graph / 权重，
# 不追求与数据集逐 token 一致（真正计量的请求由 bench 负责）。
prompt = " ".join(["hi"] * max(input_len, 1))
body = json.dumps({
    "model": model,
    "messages": [{"role": "user", "content": prompt}],
    "max_tokens": out_len,
    "temperature": 0.0,
    "stream": False,
}).encode()
headers = {"Content-Type": "application/json"}
if api_key:
    headers["Authorization"] = "Bearer " + api_key
url = base_url.rstrip("/") + "/v1/chat/completions"


def fire(_):
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            resp.read()
        return True
    except Exception as exc:
        print(f"[WARN] warmup 请求失败: {exc}")
        return False


with ThreadPoolExecutor(max_workers=max(min(conc, n), 1)) as pool:
    ok = sum(pool.map(fire, range(n)))
print(f"[warmup] 成功 {ok}/{n}")
PYEOF
    # 预热后再清一次：FLUSH_CACHE=1 时保证正式压测仍是冷启动。sglang bench 的
    # --flush-cache 就是这个语义，vllm bench 没有对应开关，故统一由脚本兜底。
    flush_server_cache
}

# 抓 BASE_URL 的 vllm prefix cache 累计计数：输出 "queries hits"（抓不到输出空串）
scrape_vllm_prefix_cache() {
    curl -s "${CURL_AUTH[@]}" "${BASE_URL}/metrics" 2>/dev/null | python3 - <<'PYEOF'
import sys
q = h = 0.0
found = False
for line in sys.stdin:
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    parts = line.split()
    if len(parts) < 2:
        continue
    name = parts[0].split("{")[0]
    try:
        val = float(parts[-1])
    except ValueError:
        continue
    if name in ("vllm:prefix_cache_queries_total", "vllm:prefix_cache_queries"):
        q += val; found = True
    elif name in ("vllm:prefix_cache_hits_total", "vllm:prefix_cache_hits"):
        h += val; found = True
if found:
    print(f"{q} {h}")
PYEOF
}

# 把 KV hit rate 注入结果 JSON。
#   $1=json 文件  $2=百分比(0-100)  $3=bench 框架（决定写哪个 key）
#   bench=sglang → 嵌套 cache_report.cache_hit_rate_pct
#   bench=vllm   → 顶层 kv_cache_hit_rate
inject_kv_hit_rate() {
    local jf="$1" pct="$2" bench_fw="$3"
    [[ -f "$jf" ]] || { echo "[WARN] 结果文件不存在，跳过 KV 注入: $jf"; return 0; }
    python3 - "$jf" "$pct" "$bench_fw" <<'PYEOF'
import sys, json
jf, pct, bench_fw = sys.argv[1], float(sys.argv[2]), sys.argv[3]
try:
    with open(jf, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception as e:
    print(f"[WARN] 读取 {jf} 失败: {e}"); sys.exit(0)
val = round(pct, 2)
if bench_fw == "sglang":
    cr = data.get("cache_report")
    if not isinstance(cr, dict):
        cr = {}
    cr["cache_hit_rate_pct"] = val
    data["cache_report"] = cr
else:
    data["kv_cache_hit_rate"] = val
with open(jf, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)
print(f"[OK] KV hit rate={val}% 已注入（key 按 bench={bench_fw}）")
PYEOF
}

# 对 vllm server 抓 KV：压测前后各一次，算 delta 注入（$1=json 文件）
capture_kv_from_vllm_server() {
    local jf="$1"
    if [[ -z "$PC_BEFORE" ]]; then
        echo "[WARN] 未取到压测前 vllm prefix cache metrics，跳过 KV（不强求）"; return 0
    fi
    local pc_after; pc_after="$(scrape_vllm_prefix_cache)"
    if [[ -z "$pc_after" ]]; then
        echo "[WARN] 未取到压测后 vllm prefix cache metrics，跳过 KV（不强求）"; return 0
    fi
    local q0 h0 q1 h1 dq dh
    q0=$(echo "$PC_BEFORE" | awk '{print $1}'); h0=$(echo "$PC_BEFORE" | awk '{print $2}')
    q1=$(echo "$pc_after"  | awk '{print $1}'); h1=$(echo "$pc_after"  | awk '{print $2}')
    dq=$(python3 -c "print($q1-$q0)"); dh=$(python3 -c "print($h1-$h0)")
    if python3 -c "import sys; sys.exit(0 if $dq>0 else 1)"; then
        local pct; pct=$(python3 -c "print($dh/$dq*100.0)")
        inject_kv_hit_rate "$jf" "$pct" "$BENCH_FRAMEWORK"
    else
        echo "[WARN] prefix_cache queries delta<=0，跳过 KV hit rate（不强求）"
    fi
}

# ---- 主循环：concurrency → input_len → prefix_rate ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INJECT_DIMS="${SCRIPT_DIR}/inject_dims.py"

for CONCURRENCY in "${max_concurrency[@]}"; do
    INPUT_LENGTHS=(${input_length_map[$CONCURRENCY]})
    for INPUT_LEN in "${INPUT_LENGTHS[@]}"; do
      for PREFIX_RATE in "${PREFIX_RATES[@]}"; do
        PROMPTS=$((CONCURRENCY * 5))
        # 文件名带 rate，避免不同 prefix 互相覆盖
        RATE_TAG=$(awk -v r="$PREFIX_RATE" 'BEGIN{printf "%.4g", r+0}')
        OUTPUT_FILE="${BASE_LOG_DIR}/${FILE_PREFIX}_${CONCURRENCY}_${INPUT_LEN}_${OUTPUT_LEN}_${PROMPTS}_pr${RATE_TAG}.json"
        if [[ -f "$OUTPUT_FILE" ]]; then
            echo "Skipping: $OUTPUT_FILE already exists."
            continue
        fi
        # 共享前缀：真实前缀长度 = round(INPUT_LEN * PREFIX_RATE)，正文 = INPUT_LEN - 前缀，
        # 保证两框架「总输入 = INPUT_LEN」一致可比。REMAIN 至少留 1，避免正文为 0。
        if awk -v r="$PREFIX_RATE" 'BEGIN{exit !(r>0)}' </dev/null 2>/dev/null; then
            HAS_PREFIX=1
            PREFIX_LEN=$(awk -v i="$INPUT_LEN" -v r="$PREFIX_RATE" 'BEGIN{printf "%d", (i*r)+0.5}')
            REMAIN=$((INPUT_LEN - PREFIX_LEN))
            if (( REMAIN < 1 )); then REMAIN=1; PREFIX_LEN=$((INPUT_LEN - 1)); fi
        else
            HAS_PREFIX=0
            PREFIX_LEN=0
            REMAIN=$INPUT_LEN
        fi

        echo -e "\n\n********************start********************"
        echo "[server=$SERVER_FRAMEWORK bench=$BENCH_FRAMEWORK] input_length=$INPUT_LEN, concurrency=$CONCURRENCY, prompts=$PROMPTS, prefix_len=$PREFIX_LEN(rate=$PREFIX_RATE)"
        echo "Output file: $OUTPUT_FILE"

        # 压测前清缓存（可选，端点按 server 选）
        flush_server_cache

        # 预热（可选）：脚本自管，跑在 KV 基线采样之前，预热流量不进 KV delta
        run_warmup "$INPUT_LEN" "$CONCURRENCY"

        # KV：server=vllm 时压测前先抓一次基线计数
        PC_BEFORE=""
        if [[ "$CAPTURE_KV" == "scrape_vllm" ]]; then
            PC_BEFORE="$(scrape_vllm_prefix_cache)"
        fi

        # ── 跑 bench（命令按 BENCH_FRAMEWORK；backend 按 SERVER_FRAMEWORK）──
        if [[ "$BENCH_FRAMEWORK" == "vllm" ]]; then
            # vllm bench serve：spec_decode_* 仅在 server=vllm 时由 bench 原生写入 JSON
            mapfile -t _VLLM_EP < <(_vllm_endpoint_args)
            vllm bench serve \
                --backend openai \
                "${_VLLM_EP[@]}" \
                --tokenizer "$TOKENIZER" \
                --model "$MODEL" \
                --dataset-name random \
                --max-concurrency "$CONCURRENCY" \
                --num-prompts "$PROMPTS" \
                --random-prefix-len "$PREFIX_LEN" \
                --random-input-len "$REMAIN" \
                --random-output-len "$OUTPUT_LEN" \
                --save-result \
                --result-dir "$BASE_LOG_DIR" \
                --result-filename "$(basename "$OUTPUT_FILE")" \
                --percentile-metrics ttft,tpot,e2el,itl \
                --metric-percentiles 90,95,99 \
                "${WARMUP_OFF[@]}"
        else
            # sglang bench：backend=sglang-oai-chat 打 sglang server / backend=vllm 打 vllm server
            #   --cache-report 仅在 server=sglang 时加（原生 KV，且只支持 sglang 后端）
            #   accept_length 仅在 backend 含 sglang（即 server=sglang）时由 bench 自动查 /server_info
            #   --output-details：写入 errors/output_lens，供 to_csv 派生 Failed 计数（不入库明细）
            #   --warmup-requests 0（WARMUP_OFF）：关掉原生默认的 1 条预热，预热改由 run_warmup 统管
            SGL_EXTRA=()
            [[ "$CAPTURE_KV" == "sglang_native" ]] && SGL_EXTRA+=(--cache-report)
            mapfile -t _SGL_EP < <(_sglang_endpoint_args)
            # 数据集选择：
            #   无前缀 → random-ids（原有行为）
            #   有前缀 → generated-shared-prefix（sglang random 无前缀参数）。
            #     逆向映射逼近 vllm 的单一全局前缀：num-groups=1（全体共享同一前缀）、
            #     prompts-per-group=PROMPTS（总请求数=PROMPTS，GSP 忽略 --num-prompts）、
            #     gsp-system-prompt-len=前缀、gsp-question-len=正文，总输入≈INPUT_LEN。
            #     另传 --random-input-len INPUT_LEN：GSP 不用它生成数据，但 bench 会把它
            #     原样回显进结果 JSON 的 random_input_len（= Input_Length 列），保证落盘正确。
            if [[ "$HAS_PREFIX" == "1" ]]; then
                SGL_DATASET_ARGS=(
                    --dataset-name generated-shared-prefix
                    --gsp-num-groups 1
                    --gsp-prompts-per-group "$PROMPTS"
                    --gsp-system-prompt-len "$PREFIX_LEN"
                    --gsp-question-len "$REMAIN"
                    --gsp-output-len "$OUTPUT_LEN"
                    --gsp-range-ratio 1
                    --random-input-len "$INPUT_LEN"
                    --random-output-len "$OUTPUT_LEN"
                )
            else
                SGL_DATASET_ARGS=(
                    --dataset-name random-ids
                    --random-input-len "$INPUT_LEN"
                    --random-output-len "$OUTPUT_LEN"
                    --random-range-ratio 1
                )
            fi
            python3 -m sglang.bench_serving --backend "$SGLANG_BENCH_BACKEND" \
                "${_SGL_EP[@]}" \
                "${SGL_DATASET_ARGS[@]}" \
                --max-concurrency "$CONCURRENCY" \
                --num-prompts "$PROMPTS" \
                --tokenizer "$TOKENIZER" \
                --output-file "$OUTPUT_FILE" \
                --output-details \
                "${WARMUP_OFF[@]}" \
                "${SGL_EXTRA[@]}" \
                --model "$MODEL"
        fi

        # ── 压测后：KV 采集（spec 已由原生 bench 写入，无需额外处理）──
        if [[ "$CAPTURE_KV" == "scrape_vllm" ]]; then
            capture_kv_from_vllm_server "$OUTPUT_FILE"
        fi

        # 注入行键维度（供 to_csv 优先读 _autores_dims）
        if [[ -f "$OUTPUT_FILE" ]]; then
            python3 "$INJECT_DIMS" "$OUTPUT_FILE" --kind text \
                --random-input-len "$INPUT_LEN" --prefix-rate "$PREFIX_RATE" \
                || echo "[WARN] inject_dims 失败（不阻断）"
        fi

        echo "********************end********************"
        sleep 10
      done
    done
done
echo -e "\n\n>>>>>>>>>>>>>>>>end-end-end>>>>>>>>>>>>>>>>"

# ── 全部压测跑完，自动落盘（to_csv.py）──
#   server / bench 框架相互独立，均显式传入；flush_cache 直接由 FLUSH_CACHE 派生，
#   与压测时的实际行为保持一致（做完压测必须调用一次，作为入库的区分维度）。
if [[ "$RUN_TO_CSV" == "1" ]]; then
    if [[ "$FLUSH_CACHE" == "1" ]]; then
        BENCH_FLUSH_CACHE="true"
    else
        BENCH_FLUSH_CACHE="false"
    fi
    TO_CSV="${SCRIPT_DIR}/to_csv.py"
    echo -e "\n[to_csv] 落盘：kind=text server=$SERVER_FRAMEWORK bench=$BENCH_FRAMEWORK flush_cache=$BENCH_FLUSH_CACHE prefix_rates=${PREFIX_RATES[*]} deployment=$DEPLOYMENT_MODE"
    # 通用参数；启动命令按部署模式追加（colocated=--launch-cmd；pd_disagg=--prefill/decode/router-cmd）
    TO_CSV_ARGS=(
        --benchmark-kind text
        --framework "$SERVER_FRAMEWORK"
        --bench-framework "$BENCH_FRAMEWORK"
        --bench-flush-cache "$BENCH_FLUSH_CACHE"
        --framework-version "$FRAMEWORK_VERSION"
        --input-dir "$BASE_LOG_DIR"
        --nas-dir "$NAS_DIR"
        --gpu-type "$GPU_TYPE"
        --model "$MODEL"
        --bench-cmd "$BENCH_CMD"
        --deployment-mode "$DEPLOYMENT_MODE"
    )
    [[ -n "$MODEL_PARAMS_B" ]]  && TO_CSV_ARGS+=(--model-params-b "$MODEL_PARAMS_B")
    [[ -n "$MODEL_WEIGHT_GB" ]] && TO_CSV_ARGS+=(--model-weight-gb "$MODEL_WEIGHT_GB")
    [[ -n "$MODEL_DTYPE" ]]     && TO_CSV_ARGS+=(--model-dtype "$MODEL_DTYPE")
    if [[ -n "$MODEL_CONFIG" ]]; then
        if [[ -f "$MODEL_CONFIG" ]]; then
            TO_CSV_ARGS+=(--model-config "$MODEL_CONFIG")
        else
            echo "[WARN] MODEL_CONFIG 指向的文件不存在，跳过：$MODEL_CONFIG"
        fi
    fi
    if [[ "$DEPLOYMENT_MODE" == "pd_disagg" ]]; then
        TO_CSV_ARGS+=(--prefill-cmd "$PREFILL_CMD" --decode-cmd "$DECODE_CMD")
        [[ -n "$ROUTER_CMD" ]] && TO_CSV_ARGS+=(--router-cmd "$ROUTER_CMD")
    else
        TO_CSV_ARGS+=(--launch-cmd "$LAUNCH_CMD")
    fi
    python3 "$TO_CSV" "${TO_CSV_ARGS[@]}" \
        || echo "[WARN] to_csv.py 落盘失败，请检查参数（可手动重跑，日志已在 $BASE_LOG_DIR）"
fi
