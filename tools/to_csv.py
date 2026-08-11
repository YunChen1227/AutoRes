#!/usr/bin/env python3
"""
性能测试结果落盘脚本 (to_csv.py)

把 sglang / vllm 的 bench 原始输出，整理成固定 schema 的 result.csv + metadata.json，
写入 NAS 上以时间戳命名的目录，供 Scanner 入库。

用法示例：
  # sglang
  python to_csv.py \
      --framework sglang \
      --framework-version 0.4.6 \
      --input-dir ./logs_H20G144_GLM52 \
      --nas-dir /mnt/nas/benchmark_root \
      --gpu-type H20-141G \
      --model GLM-4.5 --model-version distributed2 \
      --launch-cmd "python -m sglang.launch_server --tp-size 8 --enable-hierarchical-cache"

  # vllm（注意 e2el / input_len 需要额外信息，见 --bench-cmd）
  python to_csv.py \
      --framework vllm \
      --framework-version 0.5.12 \
      --input-dir ./vllm_logs \
      --nas-dir /mnt/nas/benchmark_root \
      --gpu-type H800 \
      --model Qwen2.5-72B --model-version v2.5.1 \
      --launch-cmd "vllm serve Qwen2.5-72B -tp 8 --enable-expert-parallel" \
      --bench-cmd "vllm bench serve --random-input-len 1024 --percentile-metrics ttft,tpot,itl,e2el"

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
METRIC_FIELD_MAP = {
    # 维度
    "Input_Length":        {"sglang": "random_input_len", "vllm": None},  # vllm 从 --bench-cmd 补
    "Concurrency":         {"sglang": "max_concurrency",   "vllm": "max_concurrency"},
    # 吞吐
    "Request_Throughput":  {"sglang": "request_throughput", "vllm": "request_throughput"},
    "Input_Throughput":    {"sglang": "input_throughput",   "vllm": None},  # vllm 无 input 侧吞吐
    "Output_Throughput":   {"sglang": "output_throughput",  "vllm": "output_throughput"},
    "Total_Throughput":    {"sglang": "total_throughput",   "vllm": "total_token_throughput"},
    # TTFT
    "TTFT_Mean(ms)":       {"sglang": "mean_ttft_ms",   "vllm": "mean_ttft_ms"},
    "TTFT_Median(ms)":     {"sglang": "median_ttft_ms", "vllm": "median_ttft_ms"},
    "TTFT_P95(ms)":        {"sglang": "p95_ttft_ms",    "vllm": "p95_ttft_ms"},
    "TTFT_P99(ms)":        {"sglang": "p99_ttft_ms",    "vllm": "p99_ttft_ms"},
    # TPOT
    "TPOT_Mean(ms)":       {"sglang": "mean_tpot_ms",   "vllm": "mean_tpot_ms"},
    "TPOT_Median(ms)":     {"sglang": "median_tpot_ms", "vllm": "median_tpot_ms"},
    "TPOT_P95(ms)":        {"sglang": "p95_tpot_ms",    "vllm": "p95_tpot_ms"},
    "TPOT_P99(ms)":        {"sglang": "p99_tpot_ms",    "vllm": "p99_tpot_ms"},
    # ITL
    "ITL_Mean(ms)":        {"sglang": "mean_itl_ms",    "vllm": "mean_itl_ms"},
    "ITL_Median(ms)":      {"sglang": "median_itl_ms",  "vllm": "median_itl_ms"},
    "ITL_P95(ms)":         {"sglang": "p95_itl_ms",     "vllm": "p95_itl_ms"},
    "ITL_P99(ms)":         {"sglang": "p99_itl_ms",     "vllm": "p99_itl_ms"},
    # E2E（vllm 叫 e2el）
    "E2E_Mean(ms)":        {"sglang": "mean_e2e_latency_ms",   "vllm": "mean_e2el_ms"},
    "E2E_Median(ms)":      {"sglang": "median_e2e_latency_ms", "vllm": "median_e2el_ms"},
    "E2E_P95(ms)":         {"sglang": "p95_e2e_latency_ms",    "vllm": "p95_e2el_ms"},
    "E2E_P99(ms)":         {"sglang": "p99_e2e_latency_ms",    "vllm": "p99_e2el_ms"},
    # 新增建议指标（P3 已定；不含 Duration_s）
    "Completed":           {"sglang": "completed",            "vllm": "completed"},
    "Total_Input_Tokens":  {"sglang": "total_input_tokens",   "vllm": "total_input_tokens"},
    "Total_Output_Tokens": {"sglang": "total_output_tokens",  "vllm": "total_output_tokens"},
    # ── KV cache 命中率（强制对齐：跨框架可比，统一 0-100 百分比）──
    #   sglang: bench --cache-report → cache_report.cache_hit_rate_pct（嵌套 key）
    #   vllm  : bench 本身不产出，由 vllm_sgl_benchs.sh 前后拉 /metrics 的
    #           prefix_cache_hits/queries 算 delta 注入 kv_cache_hit_rate
    "KV_Cache_Hit_Rate(%)": {"sglang": "cache_report.cache_hit_rate_pct",
                             "vllm": "kv_cache_hit_rate"},
    # ── spec decoding 接受率（不对齐：两框架颗粒度不同，跨框架不可比）──
    #   故意用带框架前缀的独立列名，避免报告把它们塞进同一可比列。
    #   sglang bench 只聚合 accept length（avg_spec_accept_length）；
    #   vllm bench 产出 acceptance_rate(%) + acceptance_length（per-position 略）。
    "SGLang_Spec_Accept_Length": {"sglang": "accept_length",              "vllm": None},
    "vLLM_Spec_Accept_Rate(%)":  {"sglang": None, "vllm": "spec_decode_acceptance_rate"},
    "vLLM_Spec_Accept_Length":   {"sglang": None, "vllm": "spec_decode_acceptance_length"},
}

# CSV 列顺序（= METRIC_FIELD_MAP 的键序）
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
import param_map as pm  # noqa: E402
import gpu_count as gc  # noqa: E402


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


def record_to_row(framework, record, fallback_input_len):
    """把一条 bench record 映射为 CSV 行（统一列名）。"""
    pm_fw = "vllm" if framework == "vllm-ascend" else framework
    row = {}
    for col, fw_keys in METRIC_FIELD_MAP.items():
        key = fw_keys.get(pm_fw)
        if key is None:
            # 该框架无此字段
            if col == "Input_Length" and pm_fw == "vllm":
                row[col] = fallback_input_len if fallback_input_len is not None else NA
            else:
                row[col] = NA
            continue
        val = _dig(record, key)
        row[col] = format_num(val) if val is not _MISSING else NA
    return row


def build_rows(framework, records, fallback_input_len):
    rows = [record_to_row(framework, r, fallback_input_len) for r in records]

    def sort_key(item):
        il = item["Input_Length"] if isinstance(item["Input_Length"], (int, float)) else 0
        cc = item["Concurrency"] if isinstance(item["Concurrency"], (int, float)) else 0
        return (il, cc)

    rows.sort(key=sort_key)
    return rows


# ============================================================================
# 4. 落盘（§5.1、§5.3）
# ============================================================================

def write_outputs(out_dir, rows, metadata):
    os.makedirs(out_dir, exist_ok=True)

    csv_path = os.path.join(out_dir, "result.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    meta_path = os.path.join(out_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    return csv_path, meta_path


def build_metadata(args, params, extra):
    """组织 metadata.json 结构（§5.3）。"""
    return {
        "framework": args.framework,
        "framework_version": args.framework_version,
        "model": args.model,
        "model_version": args.model_version,
        "gpu_type": args.gpu_type,
        "launch_cmd": args.launch_cmd,
        "bench_cmd": args.bench_cmd,
        "params": params,
        "extra": extra,
        "gpu_count": extra.get("gpu_count"),
    }


def parse_args():
    p = argparse.ArgumentParser(
        description="性能测试结果落盘脚本：整理 bench 输出为 result.csv + metadata.json"
    )
    p.add_argument("--framework", required=True, choices=["sglang", "vllm", "vllm-ascend"],
                   help="推理框架，决定 bench 字段映射与参数提取规则")
    p.add_argument("--framework-version", required=True,
                   help="框架版本（如 0.4.6 / 0.5.12），手动传入")
    p.add_argument("--input-dir", required=True,
                   help="bench 原始输出目录（sglang 为 JSONL，vllm 为多个 JSON）")
    p.add_argument("--nas-dir", required=True,
                   help="NAS 挂载根路径，脚本在其下创建时间戳目录")
    p.add_argument("--gpu-type", required=True, help="显卡类型，如 H20-141G")
    p.add_argument("--model", required=True, help="模型名")
    p.add_argument("--model-version", required=True, help="模型版本")
    p.add_argument("--launch-cmd", required=True,
                   help="完整服务启动命令字符串，用于提取 tp/dp/hicache 等参数")
    p.add_argument("--bench-cmd", default="",
                   help="完整 benchmark 命令字符串（vllm 场景用于补 random_input_len）")
    p.add_argument("--timestamp", default="",
                   help="可选，指定时间戳目录名（默认用当前时刻 YYYYMMDD_HHMMSS）")
    return p.parse_args()


def main():
    args = parse_args()

    # 时间戳目录（唯一标识）
    ts = args.timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    if not re.match(r"^\d{8}_\d{6}$", ts):
        raise SystemExit(f"[ERR] 时间戳格式非法（需 YYYYMMDD_HHMMSS）: {ts}")
    out_dir = os.path.join(args.nas_dir, ts)
    if os.path.exists(out_dir):
        raise SystemExit(f"[ERR] 目标目录已存在，避免覆盖: {out_dir}")

    # 解析 bench 输出
    records = load_bench_records(args.framework, args.input_dir)
    if not records:
        raise SystemExit(f"[ERR] 在 {args.input_dir} 未找到有效 bench 结果，请检查 --framework 与路径")

    fallback_input_len = parse_bench_cmd_input_len(args.bench_cmd)
    if args.framework in ("vllm", "vllm-ascend") and fallback_input_len is None:
        print("⚠️  vllm 场景未能从 --bench-cmd 解析出 --random-input-len，Input_Length 将填 N/A")

    rows = build_rows(args.framework, records, fallback_input_len)

    # 提取启动参数
    params, extra = extract_launch_params(args.framework, args.launch_cmd)

    # 组织并落盘
    metadata = build_metadata(args, params, extra)
    csv_path, meta_path = write_outputs(out_dir, rows, metadata)

    print(f"[OK] 落盘完成：{len(rows)} 条指标记录")
    print(f"[目录] {out_dir}")
    print(f"       - {os.path.basename(csv_path)}")
    print(f"       - {os.path.basename(meta_path)}")
    print(f"[参数] tp={params['tp']} dp={params['dp']} pp={params['pp']} "
          f"ep={params['ep']} cp={params['cp']} hicache={params['hicache_enabled']} "
          f"flexkv={params['flexkv_enabled']}")


if __name__ == "__main__":
    main()
