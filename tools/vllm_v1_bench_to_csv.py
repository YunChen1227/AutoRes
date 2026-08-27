#!/usr/bin/env python3
"""
把 vLLM V1 benchmark 输出（benchmark_summary.json + benchmark_percentile.json）
整理成 AutoRes 固定 schema 的 result.csv。

目录结构示例（每个并发点一个 parallel_* 子目录）：
  input_dir/
    **/parallel_16_number_200/benchmark_summary.json
    **/parallel_16_number_200/benchmark_percentile.json
    **/parallel_16_number_200/benchmark_args.json   # 可选，用于校验

用法：
  python tools/vllm_v1_bench_to_csv.py --input-dir ../hwj --output ../hwj/result.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys

# 复用 AutoRes 列定义与数字格式化
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.to_csv import CSV_HEADERS, NA, format_num  # noqa: E402

_PCT_MAP = {
    "50%": "Median",
    "90%": "P90",
    "95%": "P95",
    "99%": "P99",
}


def _load_json(path: str) -> dict | list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _pct_lookup(percentiles: list, metric_key: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in percentiles:
        label = row.get("Percentiles")
        suffix = _PCT_MAP.get(label)
        if not suffix:
            continue
        val = row.get(metric_key)
        if val is not None:
            out[suffix] = float(val)
    return out


def _row_from_run(run_dir: str) -> dict:
    summary_path = os.path.join(run_dir, "benchmark_summary.json")
    pct_path = os.path.join(run_dir, "benchmark_percentile.json")
    if not os.path.isfile(summary_path):
        raise FileNotFoundError(f"缺少 benchmark_summary.json: {run_dir}")

    summary = _load_json(summary_path)
    percentiles = _load_json(pct_path) if os.path.isfile(pct_path) else []

    ttft_pct = _pct_lookup(percentiles, "TTFT (ms)")
    tpot_pct = _pct_lookup(percentiles, "TPOT (ms)")
    itl_pct = _pct_lookup(percentiles, "ITL (ms)")
    # E2E 用 Latency(s) × 1000
    e2e_pct_raw = _pct_lookup(percentiles, "Latency (s)")
    e2e_pct = {k: v * 1000.0 for k, v in e2e_pct_raw.items()}

    success = int(summary.get("Success Requests") or 0)
    avg_in = float(summary.get("Avg Input Tokens") or 0)
    avg_out = float(summary.get("Avg Output Tokens") or 0)
    out_tp = float(summary.get("Output Throughput (tok/s)") or 0)
    total_tp = float(summary.get("Total Throughput (tok/s)") or 0)
    input_tp = float(summary.get("Input Throughput (tok/s)") or 0)
    if input_tp == 0.0 and total_tp and out_tp:
        input_tp = total_tp - out_tp

    spec_rate = summary.get("Spec. Accept Rate")
    spec_len = summary.get("Decoded Tok/Iter")

    row = {col: NA for col in CSV_HEADERS}
    row["Input_Length"] = format_num(avg_in)
    row["Concurrency"] = format_num(summary.get("Concurrency"))
    row["Prefix_Rate"] = NA
    row["Request_Throughput"] = format_num(summary.get("Req Throughput (req/s)"))
    row["Input_Throughput"] = format_num(input_tp)
    row["Output_Throughput"] = format_num(out_tp)
    row["Total_Throughput"] = format_num(total_tp)

    row["TTFT_Mean(ms)"] = format_num(summary.get("TTFT (ms)"))
    row["TPOT_Mean(ms)"] = format_num(summary.get("TPOT (ms)"))
    row["ITL_Mean(ms)"] = format_num(summary.get("ITL (ms)"))
    avg_lat_s = summary.get("Avg Latency (s)")
    if avg_lat_s is not None:
        row["E2E_Mean(ms)"] = format_num(float(avg_lat_s) * 1000.0)

    for prefix, pct in (("TTFT", ttft_pct), ("TPOT", tpot_pct), ("ITL", itl_pct), ("E2E", e2e_pct)):
        for suffix, val in pct.items():
            row[f"{prefix}_{suffix}(ms)"] = format_num(val)

    row["Completed"] = format_num(success)
    row["Failed"] = format_num(summary.get("Failed Requests"))
    row["Total_Input_Tokens"] = format_num(success * avg_in)
    row["Total_Output_Tokens"] = format_num(success * avg_out)
    row["KV_Cache_Hit_Rate(%)"] = NA
    row["SGLang_Spec_Accept_Length"] = NA
    if spec_rate is not None:
        row["vLLM_Spec_Accept_Rate(%)"] = format_num(float(spec_rate) * 100.0)
    if spec_len is not None:
        row["vLLM_Spec_Accept_Length"] = format_num(spec_len)

    return row


def discover_runs(input_dir: str) -> list[str]:
    pattern = os.path.join(input_dir, "**", "benchmark_summary.json")
    runs = sorted({os.path.dirname(p) for p in glob.glob(pattern, recursive=True)})
    return runs


def build_rows(input_dir: str) -> list[dict]:
    rows = []
    for run_dir in discover_runs(input_dir):
        rows.append(_row_from_run(run_dir))

    def sort_key(item):
        def _num(col):
            v = item.get(col)
            if v in (NA, None, ""):
                return (2, 0.0)
            try:
                return (0, float(v))
            except (TypeError, ValueError):
                return (1, 0.0, str(v))

        return (_num("Input_Length"), _num("Concurrency"))

    rows.sort(key=sort_key)
    return rows


def write_csv(output_path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="vLLM V1 bench JSON → AutoRes result.csv")
    parser.add_argument("--input-dir", required=True, help="包含 deepseekv4flash* 等子目录的根路径")
    parser.add_argument("--output", required=True, help="输出 CSV 路径")
    args = parser.parse_args()

    rows = build_rows(args.input_dir)
    if not rows:
        raise SystemExit(f"[ERR] 未在 {args.input_dir} 下找到 benchmark_summary.json")

    write_csv(args.output, rows)
    print(f"[OK] 写入 {len(rows)} 行 → {args.output}")


if __name__ == "__main__":
    main()
