#!/usr/bin/env python3
"""
性能测试结果落盘脚本 (to_csv.py)

把 sglang / vllm 的 bench 原始输出，整理成固定 schema 的 result.csv + metadata.json，
写入 NAS 上以时间戳命名的目录，供 Scanner 入库。

用法示例：
  # sglang（server 与 bench 同为 sglang，压测前未清缓存）
  #   metadata 字段与 autores/db/schema.py METADATA_DIRECT_FIELDS 一致
  python to_csv.py \
      --framework sglang --bench-framework sglang \
      --framework-version 0.4.6 \
      --bench-flush-cache false \
      --benchmark-kind text \
      --input-dir ./logs_H20G144_GLM52 \
      --nas-dir /mnt/nas/benchmark_root \
      --gpu-type H20-141G \
      --model GLM-4.5 \
      --launch-cmd "python -m sglang.launch_server --tp-size 8 --enable-hierarchical-cache"

  # VLM
  python to_csv.py \
      --framework sglang --bench-framework sglang \
      --framework-version 0.4.6 \
      --bench-flush-cache false \
      --benchmark-kind vlm \
      --input-dir ./vlm_logs --nas-dir /mnt/nas/benchmark_root \
      --gpu-type H800 --model Qwen2.5-VL-72B \
      --launch-cmd "python -m sglang.launch_server --tp-size 8"

设计文档见 docs/design.md §5。
"""
import os
import re
import sys
import csv
import json
import glob
import shlex
import argparse
from datetime import datetime

# Windows 控制台默认 GBK，重配为 UTF-8 避免中文/符号打印崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


# ============================================================================
# 1. 指标字段映射（§5.2）
#    把两框架 bench JSON 的 key 统一到同一套 CSV 列。
#    值为 None 表示该框架无此字段，落盘填 "N/A"。
# ============================================================================

# 统一列名 -> {框架: 该框架 JSON 里的 key}
# 维度段按 kind 拆分；性能指标段两个 kind 共用。
# 维度优先读 record["_autores_dims"].<key>（由 inject_dims.py / 脚本注入）。

_SHARED_METRIC_FIELD_MAP = {
    # 吞吐
    "Request_Throughput":  {"sglang": "request_throughput", "vllm": "request_throughput"},
    # vllm 无原生 input 侧吞吐；record_to_row 用 total_token_throughput - output_throughput 派生
    "Input_Throughput":    {"sglang": "input_throughput",   "vllm": None},
    "Output_Throughput":   {"sglang": "output_throughput",  "vllm": "output_throughput"},
    "Total_Throughput":    {"sglang": "total_throughput",   "vllm": "total_token_throughput"},
    # TTFT
    "TTFT_Mean(ms)":       {"sglang": "mean_ttft_ms",   "vllm": "mean_ttft_ms"},
    "TTFT_Median(ms)":     {"sglang": "median_ttft_ms", "vllm": "median_ttft_ms"},
    "TTFT_Std(ms)":        {"sglang": "std_ttft_ms",    "vllm": "std_ttft_ms"},
    "TTFT_P90(ms)":        {"sglang": "p90_ttft_ms",    "vllm": "p90_ttft_ms"},
    "TTFT_P95(ms)":        {"sglang": "p95_ttft_ms",    "vllm": "p95_ttft_ms"},
    "TTFT_P99(ms)":        {"sglang": "p99_ttft_ms",    "vllm": "p99_ttft_ms"},
    # TPOT
    "TPOT_Mean(ms)":       {"sglang": "mean_tpot_ms",   "vllm": "mean_tpot_ms"},
    "TPOT_Median(ms)":     {"sglang": "median_tpot_ms", "vllm": "median_tpot_ms"},
    "TPOT_Std(ms)":        {"sglang": "std_tpot_ms",    "vllm": "std_tpot_ms"},
    "TPOT_P90(ms)":        {"sglang": "p90_tpot_ms",    "vllm": "p90_tpot_ms"},
    "TPOT_P95(ms)":        {"sglang": "p95_tpot_ms",    "vllm": "p95_tpot_ms"},
    "TPOT_P99(ms)":        {"sglang": "p99_tpot_ms",    "vllm": "p99_tpot_ms"},
    # ITL
    "ITL_Mean(ms)":        {"sglang": "mean_itl_ms",    "vllm": "mean_itl_ms"},
    "ITL_Median(ms)":      {"sglang": "median_itl_ms",  "vllm": "median_itl_ms"},
    "ITL_Std(ms)":         {"sglang": "std_itl_ms",     "vllm": "std_itl_ms"},
    "ITL_P90(ms)":         {"sglang": "p90_itl_ms",     "vllm": "p90_itl_ms"},
    "ITL_P95(ms)":         {"sglang": "p95_itl_ms",     "vllm": "p95_itl_ms"},
    "ITL_P99(ms)":         {"sglang": "p99_itl_ms",     "vllm": "p99_itl_ms"},
    # E2E（vllm 叫 e2el）
    "E2E_Mean(ms)":        {"sglang": "mean_e2e_latency_ms",   "vllm": "mean_e2el_ms"},
    "E2E_Median(ms)":      {"sglang": "median_e2e_latency_ms", "vllm": "median_e2el_ms"},
    "E2E_Std(ms)":         {"sglang": "std_e2e_latency_ms",    "vllm": "std_e2el_ms"},
    "E2E_P90(ms)":         {"sglang": "p90_e2e_latency_ms",    "vllm": "p90_e2el_ms"},
    "E2E_P95(ms)":         {"sglang": "p95_e2e_latency_ms",    "vllm": "p95_e2el_ms"},
    "E2E_P99(ms)":         {"sglang": "p99_e2e_latency_ms",    "vllm": "p99_e2el_ms"},
    "Completed":           {"sglang": "completed",            "vllm": "completed"},
    "Failed":              {"sglang": None,                   "vllm": "failed_requests"},
    "Total_Input_Tokens":  {"sglang": "total_input_tokens",   "vllm": "total_input_tokens"},
    "Total_Output_Tokens": {"sglang": "total_output_tokens",  "vllm": "total_output_tokens"},
    "KV_Cache_Hit_Rate(%)": {"sglang": "cache_report.cache_hit_rate_pct",
                             "vllm": "kv_cache_hit_rate"},
    "SGLang_Spec_Accept_Length": {"sglang": "accept_length",              "vllm": None},
    "vLLM_Spec_Accept_Rate(%)":  {"sglang": None, "vllm": "spec_decode_acceptance_rate"},
    "vLLM_Spec_Accept_Length":   {"sglang": None, "vllm": "spec_decode_acceptance_length"},
}

# 行键维度：原生 JSON key（_autores_dims 优先，见 _dim_from_record）
_TEXT_DIM_FIELD_MAP = {
    "Input_Length":  {"sglang": "random_input_len", "vllm": None},
    "Concurrency":   {"sglang": "max_concurrency",   "vllm": "max_concurrency"},
    "Prefix_Rate":   {"sglang": None, "vllm": None},  # 仅来自 _autores_dims
}

_VLM_DIM_FIELD_MAP = {
    "Input_Length":      {"sglang": "random_input_len", "vllm": None},
    "Concurrency":       {"sglang": "max_concurrency",   "vllm": "max_concurrency"},
    "Image_Count":       {"sglang": None, "vllm": None},
    "Video_Count":       {"sglang": None, "vllm": None},
    "Image_Resolution":  {"sglang": None, "vllm": None},
}

# _autores_dims 内的键名（与 inject_dims.py 对齐）
_AUTORES_DIM_KEYS = {
    "Input_Length": "random_input_len",
    "Concurrency": "max_concurrency",  # 一般不需要注入，原生有
    "Prefix_Rate": "prefix_rate",
    "Image_Count": "image_count",
    "Video_Count": "video_count",
    "Image_Resolution": "image_resolution",
}


def metric_field_map_for(kind: str) -> dict:
    dims = _TEXT_DIM_FIELD_MAP if kind == "text" else _VLM_DIM_FIELD_MAP
    return {**dims, **_SHARED_METRIC_FIELD_MAP}


# 兼容旧名：默认 text
METRIC_FIELD_MAP = metric_field_map_for("text")
CSV_HEADERS = list(METRIC_FIELD_MAP.keys())

NA = "N/A"


def format_num(val):
    """数字最多保留两位小数；非数字原样返回。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return round(float(val), 2)
    return val


# 缺失标记：区别于合法的 0 / 0.0（命中率、接受率可能就是 0）
_MISSING = object()


def _dig(record, key):
    """
    从 record 取值，支持点号嵌套 key（如 cache_report.cache_hit_rate_pct）。
    命中返回值；缺失返回 _MISSING（供上层区分"没有该字段"与"值为 0"）。
    """
    cur = record
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


# ============================================================================
# 2. 启动参数提取规则（§5.4，D17）
#
#    ⚠ 2026-08-04 重写：旧版在这里手写了两段并列的
#    `if framework == "sglang" / elif "vllm"` 分支，外加一份必须手工同步的
#    flag 白名单（_collect_known_flags）。问题有三：
#      1. 默认值表与上游源码脱节，且 ep 语义写错（sglang 记 1、vllm 记 0，
#         两边都是"没开 EP"却被当成有差异，每份报告都会出现假差异）；
#      2. 同一个概念要在两段分支里各写一遍，容易只改一边；
#      3. 白名单与解析分支必须手工保持一致，漏加就会把已支持的 flag
#         误判成 unrecognized。
#
#    现在改为全部由 tools/param_map.py 的配对表驱动：flag 别名、默认值、
#    语义类型（开关 vs 宽度、极性相反、量纲不同）都在那张表里，
#    本文件只负责"按表解析"。新增参数改 param_map.py 一处即可，
#    白名单由 known_flags() 自动派生，不再手工维护。
#
#    配对表的上游基线与校验方式见 param_map.py 顶部说明；
#    升级 vllm/sglang 后请跑 `python tools/verify_param_map.py`。
# ============================================================================

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import param_map as pm  # noqa: E402
import param_map_pd as pm_pd  # noqa: E402  (PD 分离解析表，与网页上传同源)
import gpu_count as gc  # noqa: E402
from autores.db import schema as db_schema  # noqa: E402


def _iter_flag_tokens(tokens):
    """
    遍历 token 列表，产出 (flag_name, value)。

    支持三种写法：
      --flag value  /  --flag=value  /  --flag（布尔开关，value 为 True）
    布尔开关的判定：下一个 token 以 '-' 开头（或没有下一个）即视为无值。
    注意负数值（如 -1）不算 flag，需放行给上一个 flag 当取值。
    """
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if not tok.startswith("-") or _looks_like_negative_number(tok):
            i += 1
            continue
        name, sep, inline = tok.partition("=")
        if sep:
            yield name, inline
            i += 1
            continue
        nxt = tokens[i + 1] if i + 1 < n else None
        if nxt is not None and (not nxt.startswith("-") or _looks_like_negative_number(nxt)):
            yield name, nxt
            i += 2
        else:
            yield name, True
            i += 1


def _looks_like_negative_number(tok):
    """'-1' / '-0.5' 是取值不是 flag；'-tp' 是 flag。"""
    if not tok.startswith("-") or len(tok) < 2:
        return False
    return tok[1].isdigit() or (tok[1] == "." and len(tok) > 2 and tok[2].isdigit())


def _resolve_flag(name, flag_to_key):
    """
    flag 名 → 配对表 key。

    先精确匹配；未命中时按 argparse 前缀缩写规则兜底
    （SGLang 的 --tp 就是 --tp-size 的缩写，官方 cookbook 里用了 400 次，
    但它并非显式 alias，必须靠前缀匹配才能认出来）。
    仅当唯一匹配时才接受，避免歧义缩写误判。
    """
    if name in flag_to_key:
        return flag_to_key[name]
    if not name.startswith("--"):
        return None
    hits = {k for f, k in flag_to_key.items() if f.startswith(name)}
    return hits.pop() if len(hits) == 1 else None


def _coerce(kind, raw):
    """按配对表声明的语义类型把字符串取值转成合适的 Python 类型。"""
    if raw is True:  # 裸开关
        return True
    if kind == pm.K_INT:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if kind == pm.K_FLOAT:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    if kind in (pm.K_BOOL, pm.K_INVERTED, pm.K_SWITCH_WIDTH):
        if isinstance(raw, str) and raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        return raw
    return raw


# 解释器/启动噪声 flag，不算业务参数
NOISE_FLAGS = {"-m", "-c", "-u"}


def extract_launch_params(framework, launch_cmd):
    """
    从启动命令字符串提取结构化参数（由 param_map.py 配对表驱动）。

    返回 (params: dict, extra: dict)：
      params  入库顶层字段，key 与 autores/db/schema.py:PARAM_DIMENSIONS 对齐
      extra   命令里出现、但未提升为一等列的参数 + 未识别 flag

    framework 可为 vllm-ascend；参数解析走 vllm 分支，入库 framework 仍存原名。
    """
    pm_fw = "vllm" if framework == "vllm-ascend" else framework
    params = {}
    extra = {}

    if not launch_cmd:
        return params, extra

    try:
        tokens = shlex.split(launch_cmd)
    except ValueError:
        # 命令里有不成对引号等，退化为空格切分
        tokens = launch_cmd.split()

    flag_to_key = pm.flags_for(pm_fw)
    unrecognized = []
    raw = {}  # key -> 原始取值

    for name, value in _iter_flag_tokens(tokens):
        if name in NOISE_FLAGS:
            continue
        key = _resolve_flag(name, flag_to_key)
        if key is None:
            unrecognized.append(name if value is True else f"{name} {value}")
            continue
        raw[key] = value

    for key, value in raw.items():
        pair = pm.PARAM_BY_KEY[key]
        params[key] = _coerce(pair["kind"], value)

    # ── 语义归一（详见 param_map.py 各条 note）────────────────────────
    # EP：sglang 是并行宽度、vllm 是布尔开关，必须归一后才能比较
    if "ep" in params:
        enabled, width = pm.normalize_ep(pm_fw, raw["ep"])
        params.pop("ep")
        params["ep_enabled"] = enabled
        params["ep_width"] = width

    # torch_compile：sglang --enable-torch-compile 是开启；
    # vllm --enforce-eager 是关闭（极性相反）
    if "torch_compile" in params:
        params["torch_compile"] = (pm_fw == "sglang")

    # prefix_caching：sglang --disable-radix-cache 与
    # vllm --no-enable-prefix-caching 都表示关闭；--enable-prefix-caching 表示开启
    if "prefix_caching" in params:
        flags_seen = {n for n, _ in _iter_flag_tokens(tokens)}
        if "--enable-prefix-caching" in flags_seen:
            params["prefix_caching"] = True
        else:
            params["prefix_caching"] = False

    # hicache：两边机制不同（sglang 分层缓存 vs vllm KV 卸载），
    # 只标注"启用了某种 KV 分层/卸载"，具体容量等细节留在 extra
    if "hicache" in params:
        val = params["hicache"]
        params["hicache"] = True
        if val is not True:
            extra["hicache_detail"] = val

    if unrecognized:
        extra["unrecognized"] = unrecognized

    # 回填 tp/pp/dp 静态默认值并计算实际卡数（design.md D-默认值 + 卡数对齐）
    gc.annotate_gpu_count(pm_fw, params, extra)

    return params, extra


# ============================================================================
# 2b. PD 分离（Prefill-Decode Disaggregation）解析
#     与 autores/server/ingest/launch_params.extract_role / upload._build_pd 同逻辑，
#     同源复用 param_map_pd + gpu_count，保证脚本落盘的 pd 块与网页上传/入库一致。
# ============================================================================

def _pd_flag_names():
    """PD 专属 flag 名集合（从 unrecognized 里剔除，避免误报"未识别"）。"""
    return set(pm_pd.SGL_PD_FLAGS) | {pm_pd.VLLM_KV_FLAG}


def extract_role(framework, cmd):
    """
    解析一条 PD 角色（prefill 或 decode）server 命令，返回：
      {role, params, disagg, unrecognized, extra}
    role 为 'prefill'/'decode'/'both'；非 PD 角色命令 role=None（调用方据此报错）。
    """
    role = pm_pd.detect_role(framework, cmd)
    params, extra = extract_launch_params(framework, cmd)
    disagg = pm_pd.extract_disagg(framework, cmd)

    pd_flags = _pd_flag_names()
    unrecognized = []
    for item in extra.pop("unrecognized", []):
        first = item.split(" ", 1)[0]
        if first in pd_flags:
            continue
        unrecognized.append(item)

    return {
        "role": role,
        "params": params,
        "disagg": disagg,
        "unrecognized": unrecognized,
        "extra": extra,
    }


def build_pd(framework, prefill_cmd, decode_cmd, router_cmd=""):
    """
    解析 PD 分离三条命令 → (combined_launch_cmd, pd_meta)。
    pd_meta 结构与 upload._build_pd 完全一致，供 scanner._split_pd 正确入库。
    """
    pf = extract_role(framework, prefill_cmd)
    dc = extract_role(framework, decode_cmd)

    if pf["role"] not in ("prefill", "both"):
        raise SystemExit(
            "[ERR] --prefill-cmd 未检测到 prefill 角色："
            "sglang 需含 --disaggregation-mode prefill，vllm 需 kv_role=kv_producer/kv_both")
    if dc["role"] not in ("decode", "both"):
        raise SystemExit(
            "[ERR] --decode-cmd 未检测到 decode 角色："
            "sglang 需含 --disaggregation-mode decode，vllm 需 kv_role=kv_consumer/kv_both")

    router = pm_pd.parse_router(router_cmd)
    transfer_backend = (pf["disagg"].get("transfer_backend")
                        or dc["disagg"].get("transfer_backend"))

    pm_fw = "vllm" if framework == "vllm-ascend" else framework
    pf_gpus, dc_gpus, total_gpus = gc.annotate_pd_gpu_counts(
        pm_fw, pf["params"], dc["params"])

    combined = f"# PREFILL\n{prefill_cmd}\n\n# DECODE\n{decode_cmd}"
    if router_cmd:
        combined += f"\n\n# ROUTER\n{router_cmd}"

    pd_meta = {
        "transfer_backend": transfer_backend,
        "gpu_count": total_gpus,
        "prefill_gpu_count": pf_gpus,
        "decode_gpu_count": dc_gpus,
        "prefill": {
            "params": pf["params"], "launch_cmd": prefill_cmd,
            "disagg": pf["disagg"], "unrecognized": pf["unrecognized"],
            "gpu_count": pf_gpus,
        },
        "decode": {
            "params": dc["params"], "launch_cmd": decode_cmd,
            "disagg": dc["disagg"], "unrecognized": dc["unrecognized"],
            "gpu_count": dc_gpus,
        },
        "router": {**router, "launch_cmd": router_cmd},
    }
    return combined, pd_meta


def parse_bench_cmd_input_len(bench_cmd):
    """从 --bench-cmd 提取 --random-input-len（vllm 用，JSON 里没有）。返回 int 或 None。"""
    if not bench_cmd:
        return None
    try:
        tokens = shlex.split(bench_cmd)
    except ValueError:
        tokens = bench_cmd.split()
    # 注意：这里解析的是 bench 命令而非 launch 命令，与 param_map 配对表无关，
    # 因此直接复用 token 遍历，不走 flag_to_key 解析。
    for name, value in _iter_flag_tokens(tokens):
        if name == "--random-input-len" and value is not True:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


# ============================================================================
# 3. bench 输出解析（§5.2）
#    sglang: JSONL（逐行）；vllm: 每次 run 一个 JSON 文件。
# ============================================================================

def load_bench_records(framework, input_dir):
    """
    读取 input_dir 下的 bench 输出，返回原始 record dict 列表。
    sglang: 所有 *.jsonl / *.json 逐行解析（JSONL）。
    vllm:   所有 *.json 各作为一个整体 JSON。
    """
    records = []
    if framework == "sglang":
        patterns = ["*.jsonl", "*.json"]
        for pat in patterns:
            for fp in glob.glob(os.path.join(input_dir, pat)):
                with open(fp, "r", encoding="utf-8") as f:
                    for line_no, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            print(f"[WARN] 跳过无法解析的行: {fp}:{line_no}")
    else:  # vllm
        for fp in glob.glob(os.path.join(input_dir, "*.json")):
            with open(fp, "r", encoding="utf-8") as f:
                try:
                    records.append(json.load(f))
                except json.JSONDecodeError:
                    print(f"[WARN] 跳过无法解析的文件: {fp}")

    return records


def _dim_from_record(record, col, fw_key, fallback_input_len, pm_fw):
    """
    取行键维度值：优先 _autores_dims，再回落原生 JSON key / fallback。
    """
    autores = record.get("_autores_dims")
    if isinstance(autores, dict):
        ak = _AUTORES_DIM_KEYS.get(col)
        if ak and ak in autores and autores[ak] is not None:
            return format_num(autores[ak])
        # 也接受小写列名直接作键
        low = col.lower()
        if low in autores and autores[low] is not None:
            return format_num(autores[low])
    if fw_key is not None:
        val = _dig(record, fw_key)
        if val is not _MISSING:
            return format_num(val)
    if col == "Input_Length" and pm_fw == "vllm":
        return fallback_input_len if fallback_input_len is not None else NA
    return NA


def record_to_row(framework, record, fallback_input_len, kind="text"):
    """把一条 bench record 映射为 CSV 行（统一列名）。"""
    pm_fw = "vllm" if framework == "vllm-ascend" else framework
    field_map = metric_field_map_for(kind)
    dim_cols = set(db_schema.metric_dimension_keys(kind))
    row = {}
    for col, fw_keys in field_map.items():
        key = fw_keys.get(pm_fw)
        if col in dim_cols:
            row[col] = _dim_from_record(record, col, key, fallback_input_len, pm_fw)
            continue
        if key is None:
            row[col] = NA
            continue
        val = _dig(record, key)
        row[col] = format_num(val) if val is not _MISSING else NA

    # vllm 无 input 侧吞吐：total_token_throughput - output_throughput
    if pm_fw == "vllm" and row.get("Input_Throughput") in (NA, None):
        tot = _dig(record, "total_token_throughput")
        out = _dig(record, "output_throughput")
        if tot is not _MISSING and out is not _MISSING:
            try:
                row["Input_Throughput"] = format_num(float(tot) - float(out))
            except (TypeError, ValueError):
                pass

    # vllm 分位常存 percentiles_*_ms 列表（非扁平 p90_*）；扁平缺失时回填
    if pm_fw == "vllm":
        _fill_vllm_percentiles_from_lists(row, record)

    # sglang 失败数：errors → num_prompts-completed → len(output_lens)-completed → N/A
    if pm_fw == "sglang" and row.get("Failed") in (NA, None):
        comp = _dig(record, "completed")
        errs = _dig(record, "errors")
        olens = _dig(record, "output_lens")
        nprompts = _dig(record, "num_prompts")
        failed = None
        if isinstance(errs, list):
            failed = sum(1 for e in errs if e)
        elif nprompts is not _MISSING and comp is not _MISSING:
            try:
                failed = int(nprompts) - int(comp)
            except (TypeError, ValueError):
                failed = None
        elif isinstance(olens, list) and comp is not _MISSING:
            try:
                failed = len(olens) - int(comp)
            except (TypeError, ValueError):
                failed = None
        if failed is not None:
            row["Failed"] = format_num(failed)

    return row


def _fill_vllm_percentiles_from_lists(row: dict, record: dict) -> None:
    """从 percentiles_{ttft,tpot,itl,e2el}_ms 列表回填 P90/P95/P99 列。"""
    metric_to_prefix = {
        "ttft": "TTFT",
        "tpot": "TPOT",
        "itl": "ITL",
        "e2el": "E2E",
    }
    for metric, prefix in metric_to_prefix.items():
        plist = _dig(record, f"percentiles_{metric}_ms")
        if not isinstance(plist, list):
            continue
        for item in plist:
            p_val = None
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                p_val = (item[0], item[1])
            elif isinstance(item, dict):
                p = item.get("percentile", item.get("p"))
                v = item.get("value", item.get("val"))
                if p is not None and v is not None:
                    p_val = (p, v)
            if p_val is None:
                continue
            try:
                p_int = int(float(p_val[0]))
            except (TypeError, ValueError):
                continue
            col = f"{prefix}_P{p_int}(ms)"
            if col in row and row[col] in (NA, None):
                row[col] = format_num(p_val[1])


def build_rows(framework, records, fallback_input_len, kind="text"):
    rows = [record_to_row(framework, r, fallback_input_len, kind) for r in records]
    dim_cols = list(db_schema.metric_dimension_keys(kind))

    def sort_key(item):
        parts = []
        for col in dim_cols:
            v = item.get(col)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                parts.append((0, float(v), ""))
            elif v is None or v == NA:
                parts.append((2, 0, ""))
            else:
                parts.append((1, 0, str(v)))
        return tuple(parts)

    rows.sort(key=sort_key)
    return rows


# ============================================================================
# 4. 落盘（§5.1、§5.3）
# ============================================================================

def write_outputs(out_dir, rows, metadata, kind="text"):
    os.makedirs(out_dir, exist_ok=True)
    headers = list(metric_field_map_for(kind).keys())

    csv_path = os.path.join(out_dir, "result.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    meta_path = os.path.join(out_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    return csv_path, meta_path


def _cli_flag(field: str) -> str:
    """DB/metadata 字段名 → argparse CLI 名（--model_version → --model-version）。"""
    return f"--{field.replace('_', '-')}"


def _collect_metadata_from_args(args) -> dict:
    """
    从 argparse 结果收集 metadata 顶层字段。
    键集合与 schema.METADATA_DIRECT_FIELDS 一致，供 build_metadata / 校验复用。
    """
    meta = {}
    for field in db_schema.METADATA_DIRECT_FIELDS:
        if field == "bench_flush_cache":
            meta[field] = args.bench_flush_cache == "true"
        else:
            meta[field] = getattr(args, field)
    return meta


def build_metadata(meta: dict, params: dict, extra: dict,
                   bench_cmd: str = "", pd: dict | None = None) -> dict:
    """
    组织 metadata.json（§5.3）。
    顶层键仅含 schema.METADATA_DIRECT_FIELDS + 派生块 params/extra/gpu_count。
    bench_cmd 非 DB 列，若提供则写入 extra 供追溯。
    PD 分离（deployment_mode=pd_disagg）额外写 pd 块 + prefill/decode 卡数，
    结构与 persist.build_metadata 一致，供 scanner._split_pd 入库。
    """
    if bench_cmd:
        extra = dict(extra)
        extra["bench_cmd"] = bench_cmd
    out = {k: meta[k] for k in db_schema.METADATA_DIRECT_FIELDS}
    out["params"] = params
    out["extra"] = extra
    out["gpu_count"] = extra.get("gpu_count")
    if meta.get("deployment_mode") == "pd_disagg" and pd is not None:
        out["pd"] = pd
        out["gpu_count"] = pd.get("gpu_count", out["gpu_count"])
        out["prefill_gpu_count"] = pd.get("prefill_gpu_count")
        out["decode_gpu_count"] = pd.get("decode_gpu_count")
    return out


def parse_args():
    p = argparse.ArgumentParser(
        description="性能测试结果落盘脚本：整理 bench 输出为 result.csv + metadata.json。"
                    "metadata 字段与 autores/db/schema.py METADATA_DIRECT_FIELDS 对齐。"
    )
    # ── 落盘路径（非 DB 列，脚本运行参数）──
    p.add_argument("--input-dir", required=True,
                   help="bench 原始输出目录（sglang / vllm 均为 *.json）")
    p.add_argument("--nas-dir", required=True,
                   help="NAS 挂载根路径，脚本在其下创建时间戳目录")
    p.add_argument("--timestamp", default="",
                   help="可选，指定时间戳目录名（默认用当前时刻 YYYYMMDD_HHMMSS）")
    p.add_argument("--bench-cmd", default="",
                   help="（非 DB 列）vllm bench 完整命令，用于补 JSON 里没有的 Input_Length；"
                        "写入 extra.bench_cmd 供追溯")
    # ── PD 分离命令（非单独 DB 列，合并进 launch_cmd 并拆到 prefill_/decode_ 列）──
    p.add_argument("--prefill-cmd", default="",
                   help="（PD 分离必填）prefill 实例启动命令；需含角色标识"
                        "（sglang --disaggregation-mode prefill / vllm kv_role=kv_producer）")
    p.add_argument("--decode-cmd", default="",
                   help="（PD 分离必填）decode 实例启动命令；需含角色标识"
                        "（sglang --disaggregation-mode decode / vllm kv_role=kv_consumer）")
    p.add_argument("--router-cmd", default="",
                   help="（PD 分离可选）router/proxy 启动命令，解析 --policy/--prefill-policy/--decode-policy")

    # ── metadata 直接列（与 test_runs / METADATA_DIRECT_FIELDS 一一对应）──
    for field in db_schema.METADATA_DIRECT_FIELDS:
        flag = _cli_flag(field)
        if field == "framework":
            p.add_argument(flag, required=True, choices=list(db_schema.FRAMEWORK_CHOICES),
                           help="server（推理服务）框架 → test_runs.framework")
        elif field == "bench_framework":
            p.add_argument(flag, required=True, choices=list(db_schema.FRAMEWORK_CHOICES),
                           help="压测工具框架 → test_runs.bench_framework（必填，禁止默认等于 framework）")
        elif field == "bench_flush_cache":
            p.add_argument(flag, required=True, choices=["true", "false"],
                           help="压测前是否清 KV 缓存 → test_runs.bench_flush_cache")
        elif field == "deployment_mode":
            p.add_argument(flag, default=db_schema.METADATA_OPTIONAL_DEFAULTS[field],
                           choices=list(db_schema.DEPLOYMENT_MODES),
                           help="部署模式 → test_runs.deployment_mode")
        elif field == "benchmark_kind":
            p.add_argument(flag, default=db_schema.DEFAULT_BENCH_KIND,
                           choices=list(db_schema.BENCH_KIND_CHOICES),
                           help="压测类型 text|vlm → metadata.benchmark_kind（路由入库表）")
        elif field == "launch_cmd":
            # colocated 必填、pd_disagg 由 prefill/decode 合成；统一改为可选，main 里按模式校验
            p.add_argument(flag, default="",
                           help="server 启动命令原文（colocated 必填）→ test_runs.launch_cmd；"
                                "PD 分离改用 --prefill-cmd/--decode-cmd 自动合成")
        elif field in db_schema.METADATA_OPTIONAL_DEFAULTS:
            default = db_schema.METADATA_OPTIONAL_DEFAULTS[field]
            p.add_argument(flag, default=default,
                           help=f"→ test_runs.{field}（可选，默认 {default!r}）")
        elif field in db_schema.METADATA_REQUIRED:
            help_map = {
                "framework_version": "server 框架版本 → test_runs.framework_version",
                "gpu_type": "显卡型号 → test_runs.gpu_type",
                "model": "模型名 → test_runs.model",
                "launch_cmd": "server 启动命令原文 → test_runs.launch_cmd",
            }
            p.add_argument(flag, required=True, help=help_map.get(field, f"→ test_runs.{field}"))
    return p.parse_args()


def main():
    args = parse_args()
    meta = _collect_metadata_from_args(args)
    bench_framework = meta["bench_framework"]
    bench_flush_cache = meta["bench_flush_cache"]
    deployment = meta.get("deployment_mode", "colocated")
    try:
        kind = db_schema.resolve_kind(meta.get("benchmark_kind")).name
    except ValueError as e:
        raise SystemExit(f"[ERR] {e}") from e
    meta["benchmark_kind"] = kind

    # 按部署模式校验命令参数（colocated 需 launch_cmd；pd_disagg 需 prefill+decode）
    if deployment == "pd_disagg":
        if not args.prefill_cmd or not args.decode_cmd:
            raise SystemExit(
                "[ERR] --deployment-mode pd_disagg 需同时提供 --prefill-cmd 与 --decode-cmd")
    elif not meta["launch_cmd"]:
        raise SystemExit("[ERR] colocated 模式需提供 --launch-cmd")

    # 时间戳目录（唯一标识）
    ts = args.timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    if not re.match(r"^\d{8}_\d{6}$", ts):
        raise SystemExit(f"[ERR] 时间戳格式非法（需 YYYYMMDD_HHMMSS）: {ts}")
    out_dir = os.path.join(args.nas_dir, ts)
    if os.path.exists(out_dir):
        raise SystemExit(f"[ERR] 目标目录已存在，避免覆盖: {out_dir}")

    # 解析 bench 输出（按 bench 框架，因为 JSON 结构由压测工具决定）
    records = load_bench_records(bench_framework, args.input_dir)
    if not records:
        raise SystemExit(
            f"[ERR] 在 {args.input_dir} 未找到有效 bench 结果，请检查 --bench-framework 与路径")

    fallback_input_len = parse_bench_cmd_input_len(args.bench_cmd)
    if bench_framework in ("vllm", "vllm-ascend") and fallback_input_len is None:
        # 有 _autores_dims.random_input_len 时仍可补齐；此处仅告警
        has_autores = any(
            isinstance(r.get("_autores_dims"), dict)
            and r["_autores_dims"].get("random_input_len") is not None
            for r in records
        )
        if not has_autores:
            print("⚠️  vllm bench 场景未能从 --bench-cmd / _autores_dims 解析出 "
                  "random_input_len，Input_Length 将填 N/A")

    rows = build_rows(bench_framework, records, fallback_input_len, kind)

    # 提取启动参数（按 server 框架，因为 launch_cmd 是服务端启动命令）
    pd_meta = None
    if deployment == "pd_disagg":
        combined, pd_meta = build_pd(
            meta["framework"], args.prefill_cmd, args.decode_cmd, args.router_cmd)
        meta["launch_cmd"] = combined
        params = {}
        extra = {
            "gpu_count": pd_meta["gpu_count"],
            "prefill_gpu_count": pd_meta["prefill_gpu_count"],
            "decode_gpu_count": pd_meta["decode_gpu_count"],
        }
    else:
        params, extra = extract_launch_params(meta["framework"], meta["launch_cmd"])

    metadata = build_metadata(meta, params, extra, bench_cmd=args.bench_cmd, pd=pd_meta)
    csv_path, meta_path = write_outputs(out_dir, rows, metadata, kind)

    print(f"[OK] 落盘完成：{len(rows)} 条指标记录")
    print(f"[目录] {out_dir}")
    print(f"       - {os.path.basename(csv_path)}")
    print(f"       - {os.path.basename(meta_path)}")
    print(f"[bench] kind={kind} server={meta['framework']} bench={bench_framework} "
          f"flush_cache={bench_flush_cache} deployment={deployment}")
    if deployment == "pd_disagg" and pd_meta is not None:
        pf_p, dc_p = pd_meta["prefill"]["params"], pd_meta["decode"]["params"]
        print(f"[PD] transfer_backend={pd_meta['transfer_backend']}  "
              f"gpu={pd_meta['gpu_count']}"
              f"(prefill {pd_meta['prefill_gpu_count']}+decode {pd_meta['decode_gpu_count']})")
        print("[prefill] " + "  ".join(f"{k}={pf_p[k]}" for k in sorted(pf_p)))
        print("[decode]  " + "  ".join(f"{k}={dc_p[k]}" for k in sorted(dc_p)))
    else:
        if params:
            print("[参数] " + "  ".join(f"{k}={params[k]}" for k in sorted(params)))
        if extra.get("unrecognized"):
            print(f"[未识别] {' '.join(extra['unrecognized'])}")


if __name__ == "__main__":
    main()
