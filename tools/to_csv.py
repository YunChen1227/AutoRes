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


# ============================================================================
# 2. 启动参数提取规则（§5.4）
#    从 --launch-cmd 字符串解析并行度与开关，命令未写的按框架默认回填。
# ============================================================================

# 框架默认值表（§5.4.1，源码核查）
FRAMEWORK_DEFAULTS = {
    "sglang": {
        "tp": 1, "dp": 1, "pp": 1, "ep": 1, "cp": 1,
        "kv_cache_dtype": "auto",
        "hicache_enabled": False,
        "flexkv_enabled": False,
        "torch_compile": False,          # sglang 默认关，需 --enable-torch-compile
        "quantization": None,
        "attention_backend": None,       # 运行时按硬件自动选
    },
    "vllm": {
        "tp": 1, "dp": 1, "pp": 1, "ep": 0, "cp": 1,  # ep 是开关，0/1
        "kv_cache_dtype": "auto",
        "hicache_enabled": False,        # vllm 无 hicache，映射 --kv-offloading-*
        "flexkv_enabled": False,
        "torch_compile": True,           # vllm 默认开（除非 --enforce-eager）
        "quantization": None,
        "attention_backend": None,
    },
}


def _get_flag_value(tokens, flags):
    """
    在 token 列表里找 flags 中任一 flag 的取值。
    支持 "--flag value" 与 "--flag=value" 两种写法。返回 str 或 None。
    """
    for i, tok in enumerate(tokens):
        for flag in flags:
            if tok == flag:
                if i + 1 < len(tokens):
                    return tokens[i + 1]
                return None
            if tok.startswith(flag + "="):
                return tok.split("=", 1)[1]
    return None


def _has_flag(tokens, flags):
    """判断某个布尔开关 flag 是否出现（支持 --flag 与 --flag=true）。"""
    for tok in tokens:
        for flag in flags:
            if tok == flag or tok.startswith(flag + "="):
                return True
    return False


def _to_int(val, default):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def extract_launch_params(framework, launch_cmd):
    """
    从启动命令字符串提取结构化参数。
    返回 (params: dict, extra: dict)。
    params 是入库顶层字段，extra 是框架专属细节 + 未识别项。
    """
    defaults = FRAMEWORK_DEFAULTS[framework]
    params = dict(defaults)
    extra = {}

    if not launch_cmd:
        return params, extra

    try:
        tokens = shlex.split(launch_cmd)
    except ValueError:
        # 命令里有不成对引号等，退化为空格切分
        tokens = launch_cmd.split()

    if framework == "sglang":
        params["tp"] = _to_int(_get_flag_value(tokens, ["--tp-size", "--tensor-parallel-size"]), defaults["tp"])
        params["dp"] = _to_int(_get_flag_value(tokens, ["--dp-size", "--data-parallel-size"]), defaults["dp"])
        params["pp"] = _to_int(_get_flag_value(tokens, ["--pp-size", "--pipeline-parallel-size"]), defaults["pp"])
        params["ep"] = _to_int(_get_flag_value(tokens, ["--ep-size", "--ep", "--expert-parallel-size"]), defaults["ep"])
        # CP：sglang 无单一 cp-size，取 dcp（decode）为主，attention 侧记入 extra
        params["cp"] = _to_int(_get_flag_value(tokens, ["--dcp-size", "--decode-context-parallel-size"]), defaults["cp"])
        attn_cp = _get_flag_value(tokens, ["--attn-cp-size", "--attention-context-parallel-size"])
        if attn_cp is not None:
            extra["attn_cp_size"] = _to_int(attn_cp, 1)
        if _has_flag(tokens, ["--enable-prefill-cp"]):
            extra["prefill_cp"] = True

        kv_dtype = _get_flag_value(tokens, ["--kv-cache-dtype"])
        if kv_dtype is not None:
            params["kv_cache_dtype"] = kv_dtype

        params["hicache_enabled"] = _has_flag(tokens, ["--enable-hierarchical-cache"])
        if params["hicache_enabled"]:
            for key, flags in [
                ("hicache_ratio", ["--hicache-ratio"]),
                ("hicache_size", ["--hicache-size"]),
                ("hicache_write_policy", ["--hicache-write-policy"]),
                ("hicache_io_backend", ["--hicache-io-backend"]),
                ("hicache_storage_backend", ["--hicache-storage-backend"]),
            ]:
                v = _get_flag_value(tokens, flags)
                if v is not None:
                    extra[key] = v

        params["flexkv_enabled"] = _has_flag(tokens, ["--enable-flexkv"])
        params["torch_compile"] = _has_flag(tokens, ["--enable-torch-compile"])

        quant = _get_flag_value(tokens, ["--quantization"])
        if quant is not None:
            params["quantization"] = quant

        attn = _get_flag_value(tokens, ["--attention-backend"])
        if attn is not None:
            params["attention_backend"] = attn

        # 常用细节进 extra（无默认值/框架专属）
        for key, flags in [
            ("chunked_prefill_size", ["--chunked-prefill-size"]),
            ("mem_fraction_static", ["--mem-fraction-static"]),
            ("speculative_algorithm", ["--speculative-algorithm"]),
        ]:
            v = _get_flag_value(tokens, flags)
            if v is not None:
                extra[key] = v
        if _has_flag(tokens, ["--disable-radix-cache"]):
            extra["prefix_caching"] = False

    elif framework == "vllm":
        params["tp"] = _to_int(_get_flag_value(tokens, ["--tensor-parallel-size", "-tp"]), defaults["tp"])
        params["dp"] = _to_int(_get_flag_value(tokens, ["--data-parallel-size", "-dp"]), defaults["dp"])
        params["pp"] = _to_int(_get_flag_value(tokens, ["--pipeline-parallel-size", "-pp"]), defaults["pp"])
        # EP 是布尔开关，度由 TP×DP 派生；置 1/0
        params["ep"] = 1 if _has_flag(tokens, ["--enable-expert-parallel", "-ep"]) else 0
        # CP：decode-context-parallel
        params["cp"] = _to_int(_get_flag_value(tokens, ["--decode-context-parallel-size", "-dcp"]), defaults["cp"])
        pcp = _get_flag_value(tokens, ["--prefill-context-parallel-size", "-pcp"])
        if pcp is not None:
            extra["prefill_context_parallel_size"] = _to_int(pcp, 1)

        kv_dtype = _get_flag_value(tokens, ["--kv-cache-dtype"])
        if kv_dtype is not None:
            params["kv_cache_dtype"] = kv_dtype

        # vllm 无 hicache，用 --kv-offloading-* 近似
        kv_off_size = _get_flag_value(tokens, ["--kv-offloading-size"])
        if kv_off_size is not None:
            params["hicache_enabled"] = True
            extra["kv_offloading_size"] = kv_off_size
            kv_off_backend = _get_flag_value(tokens, ["--kv-offloading-backend"])
            if kv_off_backend is not None:
                extra["kv_offloading_backend"] = kv_off_backend

        # FlexKV 走 --kv-transfer-config JSON 里的 kv_connector
        kv_transfer = _get_flag_value(tokens, ["--kv-transfer-config"])
        if kv_transfer is not None:
            extra["kv_transfer_config"] = kv_transfer
            try:
                cfg = json.loads(kv_transfer)
                if str(cfg.get("kv_connector", "")).lower().startswith("flexkv"):
                    params["flexkv_enabled"] = True
            except (json.JSONDecodeError, AttributeError):
                if "flexkv" in kv_transfer.lower():
                    params["flexkv_enabled"] = True

        # torch_compile：默认开，除非 --enforce-eager
        params["torch_compile"] = not _has_flag(tokens, ["--enforce-eager"])

        quant = _get_flag_value(tokens, ["--quantization", "-q"])
        if quant is not None:
            params["quantization"] = quant

        for key, flags in [
            ("gpu_memory_utilization", ["--gpu-memory-utilization"]),
            ("max_model_len", ["--max-model-len"]),
            ("block_size", ["--block-size"]),
            ("cpu_offload_gb", ["--cpu-offload-gb"]),
        ]:
            v = _get_flag_value(tokens, flags)
            if v is not None:
                extra[key] = v
        if _has_flag(tokens, ["--no-enable-prefix-caching"]):
            extra["prefix_caching"] = False

    # 收集所有未被识别的 flag（原样存 extra.unrecognized，不丢弃）
    known_prefixes = _collect_known_flags(framework)
    # 解释器/启动噪声 flag，不算业务参数
    NOISE_FLAGS = {"-m", "-c", "-u"}
    unrecognized = []
    for tok in tokens:
        if tok.startswith("--") or (tok.startswith("-") and len(tok) > 1 and not tok[1].isdigit()):
            flag_name = tok.split("=", 1)[0]
            if flag_name in NOISE_FLAGS:
                continue
            if flag_name not in known_prefixes:
                unrecognized.append(tok)
    if unrecognized:
        extra["unrecognized"] = unrecognized

    return params, extra


def _collect_known_flags(framework):
    """已被显式处理的 flag 集合，用于识别 unrecognized。"""
    common = {
        "sglang": {
            "--tp-size", "--tensor-parallel-size", "--dp-size", "--data-parallel-size",
            "--pp-size", "--pipeline-parallel-size", "--ep-size", "--ep", "--expert-parallel-size",
            "--dcp-size", "--decode-context-parallel-size", "--attn-cp-size",
            "--attention-context-parallel-size", "--enable-prefill-cp",
            "--kv-cache-dtype", "--enable-hierarchical-cache", "--hicache-ratio", "--hicache-size",
            "--hicache-write-policy", "--hicache-io-backend", "--hicache-storage-backend",
            "--enable-flexkv", "--enable-torch-compile", "--quantization", "--attention-backend",
            "--chunked-prefill-size", "--mem-fraction-static", "--speculative-algorithm",
            "--disable-radix-cache",
        },
        "vllm": {
            "--tensor-parallel-size", "-tp", "--data-parallel-size", "-dp",
            "--pipeline-parallel-size", "-pp", "--enable-expert-parallel", "-ep",
            "--decode-context-parallel-size", "-dcp", "--prefill-context-parallel-size", "-pcp",
            "--kv-cache-dtype", "--kv-offloading-size", "--kv-offloading-backend",
            "--kv-transfer-config", "--enforce-eager", "--quantization", "-q",
            "--gpu-memory-utilization", "--max-model-len", "--block-size", "--cpu-offload-gb",
            "--no-enable-prefix-caching",
        },
    }
    return common[framework]


def parse_bench_cmd_input_len(bench_cmd):
    """从 --bench-cmd 提取 --random-input-len（vllm 用，JSON 里没有）。返回 int 或 None。"""
    if not bench_cmd:
        return None
    try:
        tokens = shlex.split(bench_cmd)
    except ValueError:
        tokens = bench_cmd.split()
    v = _get_flag_value(tokens, ["--random-input-len"])
    return _to_int(v, None)


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
    row = {}
    for col, fw_keys in METRIC_FIELD_MAP.items():
        key = fw_keys.get(framework)
        if key is None:
            # 该框架无此字段
            if col == "Input_Length" and framework == "vllm":
                row[col] = fallback_input_len if fallback_input_len is not None else NA
            else:
                row[col] = NA
            continue
        val = record.get(key, NA)
        row[col] = format_num(val) if val != NA else NA
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
    }


def parse_args():
    p = argparse.ArgumentParser(
        description="性能测试结果落盘脚本：整理 bench 输出为 result.csv + metadata.json"
    )
    p.add_argument("--framework", required=True, choices=["sglang", "vllm"],
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
    if args.framework == "vllm" and fallback_input_len is None:
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
