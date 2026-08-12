#!/bin/bash
# ============================================================================
# 压测脚本：sglang / vllm 通用，额外尝试抓取
#   1) KV cache hit rate（两框架强制对齐，跨框架可比，统一 0-100 百分比）
#   2) spec decoding 接受率/接受长度（两框架颗粒度不同，不对齐，跨框架不可比）
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
#   PD 分离下 bench 打的是 router/入口（SERVER_HOST:SERVER_PORT 填 router 地址），
#   flush / KV 需直连各实例，故另配 PREFILL_URL / DECODE_URL。
DEPLOYMENT_MODE="colocated"   # colocated | pd_disagg
# PD 分离必填（各是一条完整 server 启动命令，需含角色标识）：
#   sglang: --disaggregation-mode prefill|decode
#   vllm  : --kv-transfer-config '{"kv_role":"kv_producer|kv_consumer", ...}'
PREFILL_CMD=""
DECODE_CMD=""
ROUTER_CMD=""                 # 可选：router/proxy 命令（--policy/--prefill-policy/--decode-policy）
# PD 分离时 flush / KV 抓取直连的实例地址（BASE_URL 通常是 router，无 admin/metrics）
PREFILL_URL=""               # 例：http://30.1.1.10:18000
DECODE_URL=""                # 例：http://30.1.1.11:18000

SERVER_HOST="30.205.160.45"
SERVER_PORT="18000"

MODEL="deepseek_v4"
TOKENIZER="/mnt/pvc/pvc-sfe-platform-id10001749-vol633083-prd/llm_model/DeepSeek-V4-Flash-w8a8-mtp"

OUTPUT_LEN=1024
FLUSH_CACHE=0                 # 1=每轮压测前清 server 缓存（会把 KV hit rate 压到冷启动）

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

BASE_URL="http://${SERVER_HOST}:${SERVER_PORT}"
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
    if [[ "$FLUSH_CACHE" == "1" && -z "$PREFILL_URL" && -z "$DECODE_URL" ]]; then
        echo "[WARN] PD 分离开启 FLUSH_CACHE 但未配置 PREFILL_URL/DECODE_URL，将无法清缓存"
    fi
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

echo "================= 压测配置 ================="
echo "  SERVER_FRAMEWORK = $SERVER_FRAMEWORK"
echo "  BENCH_FRAMEWORK  = $BENCH_FRAMEWORK"
echo "  DEPLOYMENT_MODE  = $DEPLOYMENT_MODE"
echo "  BASE_URL         = $BASE_URL"
[[ "$DEPLOYMENT_MODE" == "pd_disagg" ]] && echo "  PD admin URLs    = prefill:${PREFILL_URL:-N/A}  decode:${DECODE_URL:-N/A}"
echo "  KV 采集策略      = $CAPTURE_KV"
echo "  spec 采集策略    = $CAPTURE_SPEC"
echo "  FLUSH_CACHE      = $FLUSH_CACHE"
echo "  ⚠ 落盘请执行: to_csv.py --framework $BENCH_FRAMEWORK ..."
[[ "$CAPTURE_KV" == "none" ]] && echo "  ⚠ 该组合无法可靠获取 KV hit rate（sglang server 请改用 sglang bench + --cache-report）"
[[ "$CAPTURE_SPEC" == "none" && ( "$SERVER_FRAMEWORK" != "$BENCH_FRAMEWORK" ) ]] && echo "  ⚠ server≠bench：spec 指标颗粒度错位，本轮不采集（避免误贴标签）"
echo "============================================"

# ---- 工具函数 ----

# admin/metrics 目标地址列表：
#   colocated → BASE_URL；pd_disagg → PREFILL_URL + DECODE_URL（各实例独立，需分别命中）
_admin_urls() {
    if [[ "$DEPLOYMENT_MODE" == "pd_disagg" ]]; then
        [[ -n "$PREFILL_URL" ]] && echo "$PREFILL_URL"
        [[ -n "$DECODE_URL"  ]] && echo "$DECODE_URL"
    else
        echo "$BASE_URL"
    fi
}

# 对单个 URL 清缓存（端点按 SERVER_FRAMEWORK 选）
_flush_one() {
    local url="$1"
    if [[ "$SERVER_FRAMEWORK" == "vllm" ]]; then
        curl -s -X POST "${url}/reset_prefix_cache" >/dev/null 2>&1 \
            || echo "[WARN] reset_prefix_cache 失败@${url}（vllm server 需 VLLM_SERVER_DEV_MODE=1）"
    else
        curl -s -X POST "${url}/flush_cache?timeout=60" >/dev/null 2>&1 \
            || echo "[WARN] flush_cache 失败@${url}"
    fi
}

# 清空 server 缓存（colocated 打 BASE_URL；PD 分离逐个打 prefill/decode 实例）
flush_server_cache() {
    [[ "$FLUSH_CACHE" == "1" ]] || return 0
    local urls; urls="$(_admin_urls)"
    if [[ -z "$urls" ]]; then
        echo "[WARN] 无可用 admin 地址（PD 请配置 PREFILL_URL/DECODE_URL），跳过清缓存"; return 0
    fi
    local u
    while IFS= read -r u; do
        [[ -n "$u" ]] && _flush_one "$u"
    done <<< "$urls"
}

# 抓单个 URL 的 vllm prefix cache 累计计数：输出 "queries hits"（抓不到输出空串）
_scrape_one_url() {
    curl -s "${1}/metrics" 2>/dev/null | python3 - <<'PYEOF'
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

# 汇总所有 admin 地址的 vllm prefix cache 计数（PD 分离 = prefill+decode 求和）。
# 输出 "queries hits"；任一地址都抓不到则输出空串。
scrape_vllm_prefix_cache() {
    local urls; urls="$(_admin_urls)"
    local total_q=0 total_h=0 found=0 u one q h
    while IFS= read -r u; do
        [[ -z "$u" ]] && continue
        one="$(_scrape_one_url "$u")"
        [[ -z "$one" ]] && continue
        q=$(echo "$one" | awk '{print $1}'); h=$(echo "$one" | awk '{print $2}')
        total_q=$(python3 -c "print($total_q+$q)")
        total_h=$(python3 -c "print($total_h+$h)")
        found=1
    done <<< "$urls"
    [[ "$found" == "1" ]] && echo "$total_q $total_h"
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

# ---- 主循环 ----
for CONCURRENCY in "${max_concurrency[@]}"; do
    INPUT_LENGTHS=(${input_length_map[$CONCURRENCY]})
    for INPUT_LEN in "${INPUT_LENGTHS[@]}"; do
        PROMPTS=$((CONCURRENCY * 5))
        OUTPUT_FILE="${BASE_LOG_DIR}/${FILE_PREFIX}_${CONCURRENCY}_${INPUT_LEN}_${OUTPUT_LEN}_${PROMPTS}.json"
        if [[ -f "$OUTPUT_FILE" ]]; then
            echo "Skipping: $OUTPUT_FILE already exists."
            continue
        fi
        echo -e "\n\n********************start********************"
        echo "[server=$SERVER_FRAMEWORK bench=$BENCH_FRAMEWORK] input_length=$INPUT_LEN, concurrency=$CONCURRENCY, prompts=$PROMPTS"
        echo "Output file: $OUTPUT_FILE"

        # 压测前清缓存（可选，端点按 server 选）
        flush_server_cache

        # KV：server=vllm 时压测前先抓一次基线计数
        PC_BEFORE=""
        if [[ "$CAPTURE_KV" == "scrape_vllm" ]]; then
            PC_BEFORE="$(scrape_vllm_prefix_cache)"
        fi

        # ── 跑 bench（命令按 BENCH_FRAMEWORK；backend 按 SERVER_FRAMEWORK）──
        if [[ "$BENCH_FRAMEWORK" == "vllm" ]]; then
            # vllm bench serve：spec_decode_* 仅在 server=vllm 时由 bench 原生写入 JSON
            vllm bench serve \
                --backend openai \
                --host "$SERVER_HOST" \
                --port "$SERVER_PORT" \
                --tokenizer "$TOKENIZER" \
                --model "$MODEL" \
                --dataset-name random \
                --max-concurrency "$CONCURRENCY" \
                --num-prompts "$PROMPTS" \
                --random-prefix-len 0 \
                --random-input-len "$INPUT_LEN" \
                --random-output-len "$OUTPUT_LEN" \
                --save-result \
                --result-dir "$BASE_LOG_DIR" \
                --result-filename "$(basename "$OUTPUT_FILE")" \
                --percentile-metrics ttft,tpot,e2el,itl
        else
            # sglang bench：backend=sglang-oai-chat 打 sglang server / backend=vllm 打 vllm server
            #   --cache-report 仅在 server=sglang 时加（原生 KV，且只支持 sglang 后端）
            #   accept_length 仅在 backend 含 sglang（即 server=sglang）时由 bench 自动查 /server_info
            SGL_EXTRA=()
            [[ "$CAPTURE_KV" == "sglang_native" ]] && SGL_EXTRA+=(--cache-report)
            python3 -m sglang.bench_serving --backend "$SGLANG_BENCH_BACKEND" \
                --base-url "$BASE_URL" \
                --dataset-name random-ids \
                --random-input-len "$INPUT_LEN" \
                --random-output-len "$OUTPUT_LEN" \
                --random-range-ratio 1 \
                --max-concurrency "$CONCURRENCY" \
                --num-prompts "$PROMPTS" \
                --tokenizer "$TOKENIZER" \
                --output-file "$OUTPUT_FILE" \
                --output-details \
                "${SGL_EXTRA[@]}" \
                --model "$MODEL"
        fi

        # ── 压测后：KV 采集（spec 已由原生 bench 写入，无需额外处理）──
        if [[ "$CAPTURE_KV" == "scrape_vllm" ]]; then
            capture_kv_from_vllm_server "$OUTPUT_FILE"
        fi

        echo "********************end********************"
        sleep 10
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
    TO_CSV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/to_csv.py"
    echo -e "\n[to_csv] 落盘：server=$SERVER_FRAMEWORK bench=$BENCH_FRAMEWORK flush_cache=$BENCH_FLUSH_CACHE deployment=$DEPLOYMENT_MODE"
    # 通用参数；启动命令按部署模式追加（colocated=--launch-cmd；pd_disagg=--prefill/decode/router-cmd）
    TO_CSV_ARGS=(
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
    if [[ "$DEPLOYMENT_MODE" == "pd_disagg" ]]; then
        TO_CSV_ARGS+=(--prefill-cmd "$PREFILL_CMD" --decode-cmd "$DECODE_CMD")
        [[ -n "$ROUTER_CMD" ]] && TO_CSV_ARGS+=(--router-cmd "$ROUTER_CMD")
    else
        TO_CSV_ARGS+=(--launch-cmd "$LAUNCH_CMD")
    fi
    python3 "$TO_CSV" "${TO_CSV_ARGS[@]}" \
        || echo "[WARN] to_csv.py 落盘失败，请检查参数（可手动重跑，日志已在 $BASE_LOG_DIR）"
fi
