#!/bin/bash
# ============================================================================
# 压测脚本：sglang / vllm 通用，额外尝试抓取
#   1) KV cache hit rate（两框架强制对齐，跨框架可比）
#   2) spec decoding 接受率（两框架颗粒度不同，不对齐，跨框架不可比）
#
# 关键点：
#   - FRAMEWORK 控制走哪套 bench 及指标抓取逻辑（sglang | vllm）。
#   - sglang：bench 自带 --cache-report（KV hit rate）+ /server_info 的
#     avg_spec_accept_length（accept length），都直接落进单次 JSON，无需额外抓取。
#   - vllm ：bench 自带 spec_decode_acceptance_rate/length（server 开投机解码 +
#     metrics 时自动写入 JSON）；KV hit rate vllm bench 不产出，脚本前后各拉一次
#     /metrics 的 prefix_cache_queries/hits，算 delta 注入 JSON 的 kv_cache_hit_rate。
#   - 指标"实在没有也不强求"：抓不到就跳过/留空，不阻断压测。
# ============================================================================

# ---- 框架与服务地址（可用环境变量覆盖）----
FRAMEWORK="${FRAMEWORK:-sglang}"          # sglang | vllm
SERVER_HOST="${SERVER_HOST:-30.205.160.45}"
SERVER_PORT="${SERVER_PORT:-18000}"
BASE_URL="http://${SERVER_HOST}:${SERVER_PORT}"

# 是否在每次压测前清空 server prefix cache（sglang:/flush_cache，vllm:/reset_prefix_cache）
# 注意：清缓存会把该轮 KV hit rate 压到冷启动水平；想测"自然命中率"请保持 0。
FLUSH_CACHE="${FLUSH_CACHE:-0}"

# 模型 / tokenizer（按需改）
MODEL="${MODEL:-deepseek_v4}"
TOKENIZER="${TOKENIZER:-/mnt/pvc/pvc-sfe-platform-id10001749-vol633083-prd/llm_model/DeepSeek-V4-Flash-w8a8-mtp}"

# 定义 output 长度
OUTPUT_LEN=1024
# 创建 logs 目录 (如果不存在)
LOG_SUBDIR="logs_910b_cjb_dsv4flashint8_8_260723"
mkdir -p "$LOG_SUBDIR"
# 定义并发数
declare -a max_concurrency=(8 16 32 64 128 256 512)
# 定义并发数与输入长度关系
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
# 定义基准路径 (用于生成结果文件的绝对路径)
BASE_LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${LOG_SUBDIR}"

# ---- 工具函数 ----

# 清空 server 前缀/KV 缓存（按框架选端点；失败仅告警不阻断）
flush_server_cache() {
    if [[ "$FLUSH_CACHE" != "1" ]]; then
        return 0
    fi
    if [[ "$FRAMEWORK" == "vllm" ]]; then
        # 需要 server 侧 VLLM_SERVER_DEV_MODE=1 才注册该端点
        curl -s -X POST "${BASE_URL}/reset_prefix_cache" >/dev/null 2>&1 \
            || echo "[WARN] reset_prefix_cache 失败（vllm 需 VLLM_SERVER_DEV_MODE=1）"
    else
        curl -s -X POST "${BASE_URL}/flush_cache?timeout=60" >/dev/null 2>&1 \
            || echo "[WARN] flush_cache 失败"
    fi
}

# 抓 vllm prefix cache 累计计数：输出 "queries hits"（抓不到输出空串）
scrape_vllm_prefix_cache() {
    curl -s "${BASE_URL}/metrics" 2>/dev/null | python3 - <<'PYEOF'
import sys, re
q = h = 0.0
found = False
for line in sys.stdin:
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    name = line.split("{")[0].split()[0] if line.split() else ""
    try:
        val = float(line.split()[-1])
    except (ValueError, IndexError):
        continue
    if name == "vllm:prefix_cache_queries_total" or name == "vllm:prefix_cache_queries":
        q += val; found = True
    elif name == "vllm:prefix_cache_hits_total" or name == "vllm:prefix_cache_hits":
        h += val; found = True
if found:
    print(f"{q} {h}")
PYEOF
}

# 把 kv_cache_hit_rate 注入 vllm 结果 JSON（$1=json 文件, $2=queries_delta, $3=hits_delta）
inject_vllm_kv_hit_rate() {
    local jf="$1" dq="$2" dh="$3"
    [[ -f "$jf" ]] || return 0
    python3 - "$jf" "$dq" "$dh" <<'PYEOF'
import sys, json
jf, dq, dh = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
try:
    with open(jf, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception as e:
    print(f"[WARN] 读取 {jf} 失败: {e}")
    sys.exit(0)
if dq > 0:
    data["kv_cache_hit_rate"] = round(dh / dq * 100.0, 2)
    with open(jf, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"[OK] kv_cache_hit_rate={data['kv_cache_hit_rate']}%")
else:
    print("[WARN] prefix_cache queries delta<=0，跳过 kv_cache_hit_rate")
PYEOF
}

# 运行 benchmark 测试
for CONCURRENCY in "${max_concurrency[@]}"; do
    INPUT_LENGTHS=(${input_length_map[$CONCURRENCY]})
    for INPUT_LEN in "${INPUT_LENGTHS[@]}"; do
        # ✅ 动态设置 num_prompts = CONCURRENCY * 5
        PROMPTS=$((CONCURRENCY * 5))
        # 计算输出文件路径
        OUTPUT_FILE="${BASE_LOG_DIR}/dsv4_jk_ori_${CONCURRENCY}_${INPUT_LEN}_${OUTPUT_LEN}_${PROMPTS}.json"
        # 检查结果文件是否存在, 存在则跳过
        if [[ -f "$OUTPUT_FILE" ]]; then
            echo "Skipping: $OUTPUT_FILE already exists."
            continue
        fi
        # 打印开始信息
        echo -e "\n\n********************start********************"
        echo "[$FRAMEWORK] input_length=$INPUT_LEN, concurrency=$CONCURRENCY, prompts=$PROMPTS"
        echo "Output file: $OUTPUT_FILE"

        # 压测前清缓存（可选）
        flush_server_cache

        if [[ "$FRAMEWORK" == "vllm" ]]; then
            # --- vllm 分支 ---
            # 压测前抓一次 prefix cache 计数（用于算本轮 KV hit rate 的 delta）
            PC_BEFORE="$(scrape_vllm_prefix_cache)"

            # vllm bench serve 自带 spec_decode_acceptance_rate/length（server 开投机解码时）
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

            # 压测后再抓一次，算 delta 注入 kv_cache_hit_rate
            PC_AFTER="$(scrape_vllm_prefix_cache)"
            if [[ -n "$PC_BEFORE" && -n "$PC_AFTER" ]]; then
                Q0=$(echo "$PC_BEFORE" | awk '{print $1}'); H0=$(echo "$PC_BEFORE" | awk '{print $2}')
                Q1=$(echo "$PC_AFTER"  | awk '{print $1}'); H1=$(echo "$PC_AFTER"  | awk '{print $2}')
                DQ=$(python3 -c "print($Q1-$Q0)"); DH=$(python3 -c "print($H1-$H0)")
                inject_vllm_kv_hit_rate "$OUTPUT_FILE" "$DQ" "$DH"
            else
                echo "[WARN] 未取到 vllm prefix cache metrics，跳过 KV hit rate（不强求）"
            fi
        else
            # --- sglang 分支 ---
            # --cache-report：产出 cache_report.cache_hit_rate_pct（KV hit rate）
            #   sglang-oai-chat 后端需 server 启动带 --enable-cache-report
            # accept length：backend 含 sglang 时 bench 自动查 /server_info 的
            #   avg_spec_accept_length 并写入 JSON 的 accept_length
            python3 -m sglang.bench_serving --backend sglang-oai-chat \
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
                --cache-report \
                --model "$MODEL"
        fi

        echo "********************end********************"
        sleep 10
    done
done
echo -e "\n\n>>>>>>>>>>>>>>>>end-end-end>>>>>>>>>>>>>>>>"
