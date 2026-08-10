"""
解析器：读取一个 timestamp 目录下的 result.csv + metadata.json，
组织为 test_runs 文档（design.md §5.5、§6.1）。

面向 to_csv.py 生成的固定 schema，不做可配置字段映射。
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone

from autores.db import schema

NA_VALUES = {"N/A", "n/a", "NA", "", None}


class ParseError(Exception):
    """解析失败：目录不完整或格式非法。上层据此跳过、不入库。"""


def _parse_timestamp(dir_name: str) -> datetime:
    """由目录名 YYYYMMDD_HHMMSS 解析为 datetime。"""
    try:
        return datetime.strptime(dir_name, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise ParseError(f"目录名不符合时间戳格式: {dir_name}") from e


def _to_number(raw: str):
    """把 CSV 单元格转为 float/int；N/A 或空转为 None。"""
    if raw in NA_VALUES:
        return None
    try:
        f = float(raw)
        # 整数值去掉小数点（Input_Length/Concurrency/Completed 等）
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return raw  # 非数字原样保留（理论上不该出现）


def _parse_csv(csv_path: str) -> list[dict]:
    """把 result.csv 每行转为一个 metric 记录（含 input_length/concurrency 维度）。"""
    if not os.path.exists(csv_path):
        raise ParseError(f"缺少 result.csv: {csv_path}")

    metrics: list[dict] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ParseError(f"result.csv 无表头: {csv_path}")
        for row in reader:
            record: dict = {}
            for col, raw in row.items():
                if col is None:
                    continue
                # Input_Length -> input_length，Concurrency -> concurrency（维度用小写）
                if col in schema.METRIC_DIMENSION_KEYS:
                    key = col.lower()
                else:
                    key = col
                record[key] = _to_number(raw)
            metrics.append(record)

    if not metrics:
        raise ParseError(f"result.csv 无数据行: {csv_path}")
    return metrics


def _parse_metadata(meta_path: str) -> dict:
    if not os.path.exists(meta_path):
        raise ParseError(f"缺少 metadata.json: {meta_path}")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError as e:
        raise ParseError(f"metadata.json 解析失败: {meta_path}: {e}") from e

    # 校验必备字段
    required = ["framework", "framework_version", "model", "model_version",
                "gpu_type", "launch_cmd", "params"]
    missing = [k for k in required if k not in meta]
    if missing:
        raise ParseError(f"metadata.json 缺字段 {missing}: {meta_path}")
    return meta


def parse_run_dir(dir_path: str) -> dict:
    """
    解析单个 timestamp 目录，返回可直接 insert_one 的 test_runs 文档。
    失败抛 ParseError（上层跳过、不入库、下轮重试）。
    """
    dir_name = os.path.basename(os.path.normpath(dir_path))
    run_timestamp = _parse_timestamp(dir_name)

    meta = _parse_metadata(os.path.join(dir_path, "metadata.json"))
    metrics = _parse_csv(os.path.join(dir_path, "result.csv"))

    deployment = meta.get("deployment_mode", "colocated")
    extra = dict(meta.get("extra", {}))

    doc = {
        "_id": dir_name,
        "run_timestamp": run_timestamp,
        "model": meta["model"],
        "model_version": meta["model_version"],
        "framework": meta["framework"],
        "framework_version": meta["framework_version"],
        "gpu_type": meta["gpu_type"],
        "launch_cmd": meta["launch_cmd"],
        "deployment_mode": deployment,
        "gpu_count": meta.get("gpu_count") or extra.get("gpu_count"),
        "prefill_gpu_count": meta.get("prefill_gpu_count"),
        "decode_gpu_count": meta.get("decode_gpu_count"),
        "params": meta.get("params", {}),
        "extra": extra,
        "metrics": metrics,
        "created_at": datetime.now(timezone.utc),
    }

    if deployment == "pd_disagg" and meta.get("pd"):
        doc["pd"], extra["pd"] = _split_pd(meta["pd"])
    return doc


def _split_pd(pd_meta: dict) -> tuple[dict, dict]:
    """
    metadata.pd → (doc.pd 列数据, extra.pd 原文留档)。
      doc.pd  : 提列存储/对比用（transfer_backend / 各角色 params / router 策略）
      extra.pd: 原始启动命令、PD 专属字段、未识别 flag，供展示与追溯
    """
    prefill = pd_meta.get("prefill", {}) or {}
    decode = pd_meta.get("decode", {}) or {}
    router = pd_meta.get("router", {}) or {}

    doc_pd = {
        "transfer_backend": pd_meta.get("transfer_backend"),
        "prefill": {"params": prefill.get("params", {})},
        "decode": {"params": decode.get("params", {})},
        "router": {
            "policy": router.get("policy"),
            "prefill_policy": router.get("prefill_policy"),
            "decode_policy": router.get("decode_policy"),
        },
    }
    extra_pd = {
        "prefill": {k: prefill.get(k) for k in ("launch_cmd", "disagg", "unrecognized")},
        "decode": {k: decode.get(k) for k in ("launch_cmd", "disagg", "unrecognized")},
        "router": {k: router.get(k) for k in ("launch_cmd", "_extra")},
    }
    return doc_pd, extra_pd
