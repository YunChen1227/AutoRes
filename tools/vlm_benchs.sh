#!/bin/bash
# ============================================================================
# VLM 多模态压测脚本：sglang / vllm 通用
#   行键：input_length × concurrency × image_count × video_count × image_resolution
#   （video_count 为保留标量，>0 直接报错退出）
#
# ★ SERVER_FRAMEWORK / BENCH_FRAMEWORK 语义与 vllm_sgl_benchs.sh 相同。
# ★ 两框架 MM 参数映射（统一用 HxW 字符串）：
#     sglang: --dataset-name image --image-count N --image-resolution HxW
#             --image-format jpeg --image-content random
#     vllm:   --dataset-name random-mm
#             --random-mm-base-items-per-request N
#             --random-mm-num-mm-items-range-ratio 0
#             --random-mm-limit-mm-per-prompt '{"image": N, "video": 0}'
#             --random-mm-bucket-config '{"(H, W, 1)": 1.0}'
# ★ 启动时用 --help 探测上述 flag；缺失则硬失败（不静默降级）。
# ============================================================================

# ┌──────────────────────── 配置区（改这里，不再传参）────────────────────────┐
SERVER_FRAMEWORK="sglang"     # 推理服务框架： sglang | vllm
BENCH_FRAMEWORK="sglang"      # 压测工具框架： sglang | vllm

DEPLOYMENT_MODE="colocated"   # colocated | pd_disagg
PREFILL_CMD=""
DECODE_CMD=""
ROUTER_CMD=""

ENDPOINT_MODE="host_port"    # host_port | url
SERVER_HOST="30.205.160.45"
SERVER_PORT="18000"
SERVER_URL=""
API_KEY=""

MODEL="qwen2_vl"
TOKENIZER="/path/to/tokenizer"

OUTPUT_LEN=128
FLUSH_CACHE=0

WARMUP_REQUESTS=0
WARMUP_OUTPUT_LEN=32

# ── VLM 行键（第 3/4 层循环）──
# VIDEO_COUNT 仅作保留字段；>0 直接报错（sglang image 数据集无视频合成能力）
VIDEO_COUNT=0
declare -a IMAGE_COUNTS=(1)
declare -a IMAGE_RESOLUTIONS=("720x1280")   # 统一 HxW

LOG_SUBDIR="logs_vlm"
FILE_PREFIX="vlm"

RUN_TO_CSV=1
NAS_DIR="/mnt/nas/benchmark_root"
FRAMEWORK_VERSION="0.4.6"
GPU_TYPE="910B4-64G"
LAUNCH_CMD="python -m sglang.launch_server --tp-size 8"
BENCH_CMD=""

declare -a max_concurrency=(1 2 4 8 16)
declare -A input_length_map=(
    [1]="128 256 512 1024"
    [2]="128 256 512 1024"
    [4]="128 256 512 1024"
    [8]="128 256 512"
    [16]="128 256 512"
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

# ── VLM 维度校验 ──
if ! [[ "$VIDEO_COUNT" =~ ^[0-9]+$ ]]; then
    echo "[ERR] VIDEO_COUNT 必须是非负整数，当前=$VIDEO_COUNT" >&2
    exit 1
fi
if (( VIDEO_COUNT > 0 )); then
    echo "[ERR] VIDEO_COUNT>0 暂不支持（sglang image 数据集无视频合成；仅作保留字段）当前=$VIDEO_COUNT" >&2
    exit 1
fi
if (( ${#IMAGE_COUNTS[@]} == 0 )); then
    echo "[ERR] IMAGE_COUNTS 不能为空" >&2
    exit 1
fi
if (( ${#IMAGE_RESOLUTIONS[@]} == 0 )); then
    echo "[ERR] IMAGE_RESOLUTIONS 不能为空" >&2
    exit 1
fi
for _ic in "${IMAGE_COUNTS[@]}"; do
    if ! [[ "$_ic" =~ ^[1-9][0-9]*$ ]]; then
        echo "[ERR] IMAGE_COUNTS 每项必须是正整数，当前=$_ic" >&2
        exit 1
    fi
done
for _res in "${IMAGE_RESOLUTIONS[@]}"; do
    if ! [[ "$_res" =~ ^[0-9]+[xX][0-9]+$ ]]; then
        echo "[ERR] IMAGE_RESOLUTIONS 每项必须是 HxW（如 720x1280），当前=$_res" >&2
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

# ── 探测 bench 是否支持关闭原生 warmup + VLM 必需 flag（缺失硬失败）──
if [[ "$BENCH_FRAMEWORK" == "vllm" ]]; then
    BENCH_WARMUP_FLAG="--num-warmups"
    BENCH_WARMUP_HELP="$(vllm bench serve --help 2>/dev/null)"
    REQUIRED_FLAGS=(
        --dataset-name
        --random-mm-base-items-per-request
        --random-mm-num-mm-items-range-ratio
        --random-mm-limit-mm-per-prompt
        --random-mm-bucket-config
    )
else
    BENCH_WARMUP_FLAG="--warmup-requests"
    BENCH_WARMUP_HELP="$(python3 -m sglang.bench_serving --help 2>/dev/null)"
    REQUIRED_FLAGS=(
        --dataset-name
        --image-count
        --image-resolution
        --image-format
        --image-content
    )
fi
WARMUP_OFF=()
if grep -q -- "$BENCH_WARMUP_FLAG" <<<"$BENCH_WARMUP_HELP"; then
    BENCH_WARMUP_FLAG_OK=1
    WARMUP_OFF=("$BENCH_WARMUP_FLAG" 0)
else
    BENCH_WARMUP_FLAG_OK=0
fi
for _flag in "${REQUIRED_FLAGS[@]}"; do
    if ! grep -q -- "$_flag" <<<"$BENCH_WARMUP_HELP"; then
        echo "[ERR] 当前 $BENCH_FRAMEWORK bench 不支持必需 flag: $_flag" >&2
        echo "      请升级 bench 版本后重试（不做静默降级）" >&2
        exit 1
    fi
done

echo "================= VLM 压测配置 ================="
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
    echo "  WARMUP_REQUESTS  = $WARMUP_REQUESTS  (脚本自管带图预热；bench 原生 warmup 关闭)"
else
    echo "  WARMUP_REQUESTS  = 0  (不预热)"
fi
echo "  IMAGE_COUNTS     = ${IMAGE_COUNTS[*]}"
echo "  IMAGE_RESOLUTIONS= ${IMAGE_RESOLUTIONS[*]}"
echo "  VIDEO_COUNT      = $VIDEO_COUNT  (保留字段，必须为 0)"
echo "  ⚠ 落盘请执行: to_csv.py --benchmark-kind vlm --framework $BENCH_FRAMEWORK ..."
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

# 预热：带图 chat，让视觉编码器也预热。
#   $1=input_len  $2=concurrency  $3=image_count  $4=image_resolution(HxW)
run_warmup() {
    local input_len="$1" conc="$2" img_count="$3" img_res="$4"
    (( WARMUP_REQUESTS > 0 )) || return 0
    echo "[warmup] ${WARMUP_REQUESTS} 条（input≈${input_len} token，images=${img_count}@${img_res}，max_tokens=${WARMUP_OUTPUT_LEN}，并发≤${conc}）"
    python3 - "$BASE_URL" "$MODEL" "$input_len" "$WARMUP_OUTPUT_LEN" \
             "$WARMUP_REQUESTS" "$conc" "${API_KEY}" "$img_count" "$img_res" <<'PYEOF'
import base64
import io
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

base_url, model, input_len, out_len, n, conc, api_key, img_count, img_res = sys.argv[1:10]
input_len, out_len, n, conc = int(input_len), int(out_len), int(n), int(conc)
img_count = int(img_count)
h_str, w_str = img_res.lower().split("x", 1)
h, w = int(h_str), int(w_str)

def make_jpeg_b64(height, width):
    try:
        from PIL import Image  # type: ignore
        import random
        img = Image.new("RGB", (width, height))
        pixels = img.load()
        for y in range(height):
            for x in range(width):
                pixels[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        print(f"[WARN] PIL 不可用或生成失败({exc})，退化为固定 8x8 JPEG")
        # minimal valid JPEG
        tiny = bytes([
            0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
            0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
            0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
            0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
            0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
            0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
            0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
            0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x08,
            0x00, 0x08, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
            0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
            0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
            0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
            0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
            0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
            0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
            0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
            0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
            0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
            0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
            0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
            0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
            0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
            0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
            0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
            0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
            0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
            0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD5, 0xDB, 0x20, 0xA8, 0xF1, 0x45, 0x00,
            0xFF, 0xD9,
        ])
        return base64.b64encode(tiny).decode("ascii")

b64 = make_jpeg_b64(h, w)
data_url = "data:image/jpeg;base64," + b64
prompt = " ".join(["hi"] * max(input_len, 1))
content = [{"type": "text", "text": prompt}]
for _ in range(max(img_count, 0)):
    content.append({"type": "image_url", "image_url": {"url": data_url}})
body = json.dumps({
    "model": model,
    "messages": [{"role": "user", "content": content}],
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

# ---- 主循环：concurrency → input_len → image_count → image_resolution ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INJECT_DIMS="${SCRIPT_DIR}/inject_dims.py"

for CONCURRENCY in "${max_concurrency[@]}"; do
    INPUT_LENGTHS=(${input_length_map[$CONCURRENCY]})
    for INPUT_LEN in "${INPUT_LENGTHS[@]}"; do
      for IMAGE_COUNT in "${IMAGE_COUNTS[@]}"; do
        for IMAGE_RESOLUTION in "${IMAGE_RESOLUTIONS[@]}"; do
        PROMPTS=$((CONCURRENCY * 5))
        RES_TAG="${IMAGE_RESOLUTION,,}"   # lower-case HxW
        OUTPUT_FILE="${BASE_LOG_DIR}/${FILE_PREFIX}_${CONCURRENCY}_${INPUT_LEN}_${OUTPUT_LEN}_${PROMPTS}_img${IMAGE_COUNT}_${RES_TAG}.json"
        if [[ -f "$OUTPUT_FILE" ]]; then
            echo "Skipping: $OUTPUT_FILE already exists."
            continue
        fi

        # HxW → H,W for vllm bucket-config
        IMG_H="${IMAGE_RESOLUTION%%[xX]*}"
        IMG_W="${IMAGE_RESOLUTION##*[xX]}"
        VLLM_BUCKET="{\"(${IMG_H}, ${IMG_W}, 1)\": 1.0}"
        VLLM_LIMIT="{\"image\": ${IMAGE_COUNT}, \"video\": 0}"

        echo -e "\n\n********************start********************"
        echo "[server=$SERVER_FRAMEWORK bench=$BENCH_FRAMEWORK] input_length=$INPUT_LEN, concurrency=$CONCURRENCY, prompts=$PROMPTS, image_count=$IMAGE_COUNT, video_count=$VIDEO_COUNT, resolution=$IMAGE_RESOLUTION"
        echo "Output file: $OUTPUT_FILE"

        flush_server_cache
        run_warmup "$INPUT_LEN" "$CONCURRENCY" "$IMAGE_COUNT" "$IMAGE_RESOLUTION"

        PC_BEFORE=""
        if [[ "$CAPTURE_KV" == "scrape_vllm" ]]; then
            PC_BEFORE="$(scrape_vllm_prefix_cache)"
        fi

        if [[ "$BENCH_FRAMEWORK" == "vllm" ]]; then
            mapfile -t _VLLM_EP < <(_vllm_endpoint_args)
            vllm bench serve \
                --backend openai-chat \
                "${_VLLM_EP[@]}" \
                --tokenizer "$TOKENIZER" \
                --model "$MODEL" \
                --dataset-name random-mm \
                --max-concurrency "$CONCURRENCY" \
                --num-prompts "$PROMPTS" \
                --random-input-len "$INPUT_LEN" \
                --random-output-len "$OUTPUT_LEN" \
                --random-mm-base-items-per-request "$IMAGE_COUNT" \
                --random-mm-num-mm-items-range-ratio 0 \
                --random-mm-limit-mm-per-prompt "$VLLM_LIMIT" \
                --random-mm-bucket-config "$VLLM_BUCKET" \
                --save-result \
                --result-dir "$BASE_LOG_DIR" \
                --result-filename "$(basename "$OUTPUT_FILE")" \
                --percentile-metrics ttft,tpot,e2el,itl \
                --metric-percentiles 90,95,99 \
                "${WARMUP_OFF[@]}"
        else
            SGL_EXTRA=()
            [[ "$CAPTURE_KV" == "sglang_native" ]] && SGL_EXTRA+=(--cache-report)
            mapfile -t _SGL_EP < <(_sglang_endpoint_args)
            python3 -m sglang.bench_serving --backend "$SGLANG_BENCH_BACKEND" \
                "${_SGL_EP[@]}" \
                --dataset-name image \
                --image-count "$IMAGE_COUNT" \
                --image-resolution "$IMAGE_RESOLUTION" \
                --image-format jpeg \
                --image-content random \
                --random-input-len "$INPUT_LEN" \
                --random-output-len "$OUTPUT_LEN" \
                --random-range-ratio 1 \
                --max-concurrency "$CONCURRENCY" \
                --num-prompts "$PROMPTS" \
                --tokenizer "$TOKENIZER" \
                --output-file "$OUTPUT_FILE" \
                --output-details \
                "${WARMUP_OFF[@]}" \
                "${SGL_EXTRA[@]}" \
                --model "$MODEL"
        fi

        if [[ "$CAPTURE_KV" == "scrape_vllm" ]]; then
            capture_kv_from_vllm_server "$OUTPUT_FILE"
        fi

        if [[ -f "$OUTPUT_FILE" ]]; then
            python3 "$INJECT_DIMS" "$OUTPUT_FILE" --kind vlm \
                --random-input-len "$INPUT_LEN" \
                --image-count "$IMAGE_COUNT" \
                --video-count "$VIDEO_COUNT" \
                --image-resolution "$IMAGE_RESOLUTION" \
                || echo "[WARN] inject_dims 失败（不阻断）"
        fi

        echo "********************end********************"
        sleep 10
        done
      done
    done
done
echo -e "\n\n>>>>>>>>>>>>>>>>end-end-end>>>>>>>>>>>>>>>>"

# ── 全部压测跑完，自动落盘（to_csv.py）──
if [[ "$RUN_TO_CSV" == "1" ]]; then
    if [[ "$FLUSH_CACHE" == "1" ]]; then
        BENCH_FLUSH_CACHE="true"
    else
        BENCH_FLUSH_CACHE="false"
    fi
    TO_CSV="${SCRIPT_DIR}/to_csv.py"
    echo -e "\n[to_csv] 落盘：kind=vlm server=$SERVER_FRAMEWORK bench=$BENCH_FRAMEWORK flush_cache=$BENCH_FLUSH_CACHE images=${IMAGE_COUNTS[*]} res=${IMAGE_RESOLUTIONS[*]} deployment=$DEPLOYMENT_MODE"
    TO_CSV_ARGS=(
        --benchmark-kind vlm
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
