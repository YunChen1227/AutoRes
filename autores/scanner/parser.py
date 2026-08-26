"""
解析器：读取一个 timestamp 目录下的 result.csv + metadata.json，
组织为 test_runs / vlm_test_runs 文档（design.md §5.5、§6.1）。

面向 to_csv.py 生成的固定 schema，不做可配置字段映射。
路由由 metadata.benchmark_kind 决定（缺省 text）。

关于目录里的 model_config.json（模型 config 原文）：
  启动参数的推导发生在**入库前**（to_csv.py 落盘 / upload 提交时），结果已经
  写进 metadata.params 与 metadata.extra。本模块不重跑推导——同一个目录在不同
  AutoRes 版本下必须解析出同一行数据，否则台账会随代码升级悄悄漂移。
  只有一种例外：目录里有 config 原文、但 metadata 里没带 model_arch（老目录或
  手工拼的目录），此时补一份模型结构字段，供分析层使用。这只加字段、不动 params。
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
        return raw  # 非数字原样保留（如 Image_Resolution=720x1280）


def _parse_csv(csv_path: str, kind: str) -> list[dict]:
    """把 result.csv 每行转为一个 metric 记录（行键维度用小写）。"""
    if not os.path.exists(csv_path):
        raise ParseError(f"缺少 result.csv: {csv_path}")

    dim_keys = set(schema.metric_dimension_keys(kind))
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
                # Input_Length -> input_length 等（维度用小写）
                if col in dim_keys:
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

    # 必备字段（Scanner 读 NAS 老目录：bench 字段后加，缺了仍允许入库，列留 NULL）
    required = ["model", "framework", "framework_version", "gpu_type", "launch_cmd", "params"]
    missing = [k for k in required if k not in meta]
    if missing:
        raise ParseError(f"metadata.json 缺字段 {missing}: {meta_path}")
    for field, default in schema.METADATA_OPTIONAL_DEFAULTS.items():
        meta.setdefault(field, default)
    return meta


def _load_model_arch(dir_path: str) -> dict | None:
    """
    目录里有模型 config 原文时，归一成 model_arch 字段（层数/KV 头数/head_dim 等）。

    仅在 metadata.extra 没带 model_arch 时调用。解析不了就返回 None——
    这是补充信息，不能因为它让整个目录入库失败。
    """
    from autores.server.ingest import launch_params

    path = os.path.join(dir_path, launch_params.model_config_filename())
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            cfg = launch_params.load_model_config(f.read())
        return launch_params.normalize_model_config(cfg)
    except (OSError, ValueError):
        return None


def parse_run_dir(dir_path: str) -> dict:
    """
    解析单个 timestamp 目录，返回可直接 insert_run 的文档。
    失败抛 ParseError（上层跳过、不入库、下轮重试）。
    """
    dir_name = os.path.basename(os.path.normpath(dir_path))
    run_timestamp = _parse_timestamp(dir_name)

    meta = _parse_metadata(os.path.join(dir_path, "metadata.json"))
    try:
        kind = schema.resolve_kind(meta.get("benchmark_kind")).name
    except ValueError as e:
        raise ParseError(str(e)) from e
    metrics = _parse_csv(os.path.join(dir_path, "result.csv"), kind)

    deployment = meta.get("deployment_mode", "colocated")
    extra = dict(meta.get("extra", {}))
    if not extra.get("model_arch"):
        arch = _load_model_arch(dir_path)
        if arch:
            extra["model_arch"] = arch

    doc = {
        "_id": dir_name,
        "run_timestamp": run_timestamp,
        "model": meta["model"],
        "model_version": meta["model_version"],
        # 老 NAS 目录的 metadata.json 里没有这几个键（字段是后加的），列可空。
        # 已废弃的 model_size 口径不清（GB 还是 B 说不准），刻意不做换算回填。
        "model_params_b": meta.get("model_params_b"),
        "model_weight_gb": meta.get("model_weight_gb"),
        "model_dtype": meta.get("model_dtype"),
        "framework": meta["framework"],
        "framework_version": meta["framework_version"],
        "gpu_type": meta["gpu_type"],
        "launch_cmd": meta["launch_cmd"],
        "deployment_mode": deployment,
        "bench_framework": meta.get("bench_framework"),
        "bench_flush_cache": meta.get("bench_flush_cache"),
        "benchmark_kind": kind,
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
    role_keys = ("launch_cmd", "disagg", "unrecognized",
                 "params_explicit", "param_sources")
    extra_pd = {
        "prefill": {k: prefill.get(k) for k in role_keys},
        "decode": {k: decode.get(k) for k in role_keys},
        "router": {k: router.get(k) for k in ("launch_cmd", "_extra")},
    }
    return doc_pd, extra_pd
