"""
SQLite 表结构定义与共享常量（design.md §6）。

test_runs 表：一次测试 = 一行。
  run_id            : TEXT PRIMARY KEY（= timestamp 目录名，天然唯一/幂等）
  run_timestamp     : TEXT（ISO 8601）
  model / model_version / framework / framework_version / gpu_type / launch_cmd : TEXT
  tp/dp/pp/ep/cp    : INTEGER（结构化启动参数，独立列，D18）
  kv_cache_dtype / quantization / attention_backend : TEXT
  hicache_enabled / flexkv_enabled / torch_compile  : INTEGER (0/1)
  extra             : TEXT（JSON，框架专属细节 + 未识别参数）
  metrics           : TEXT（JSON 数组，每个 (input_length, concurrency) 组合一条；
                       对应此前"内嵌数组"建模——查询只按维度筛选，指标整取后在
                       Python 侧透视，无需拆表）
  created_at        : TEXT（ISO 8601）

ingest_log 表：已入库目录台账。
  source_dir        : TEXT PRIMARY KEY（= timestamp 目录名）
  run_id            : TEXT
  ingested_at       : TEXT（ISO 8601）
"""
from __future__ import annotations

import json
from datetime import datetime

# ── 元信息维度（test_runs 直接列）──
META_DIMENSIONS = [
    "model",
    "model_version",
    "framework",
    "framework_version",
    "gpu_type",
]

# ── 结构化启动参数维度（同样是直接列；文档/接口上仍归为 params）──
PARAM_DIMENSIONS = [
    "tp",
    "dp",
    "pp",
    "ep",
    "cp",
    "kv_cache_dtype",
    "hicache_enabled",
    "flexkv_enabled",
    "torch_compile",
    "quantization",
    "attention_backend",
]

# 布尔型参数列（SQLite 存 0/1，读出时还原为 bool）
BOOL_PARAMS = {"hicache_enabled", "flexkv_enabled", "torch_compile"}

# Agent 可用于对比/筛选的全部维度（design.md §7.2 list_dimension_values 枚举）
ALL_DIMENSIONS = META_DIMENSIONS + PARAM_DIMENSIONS

# 指标里的两个"维度列"（不是性能数值，而是测试条件）
METRIC_DIMENSION_KEYS = ["Input_Length", "Concurrency"]

DDL = """
CREATE TABLE IF NOT EXISTS test_runs (
    run_id            TEXT PRIMARY KEY,
    run_timestamp     TEXT NOT NULL,
    model             TEXT NOT NULL,
    model_version     TEXT NOT NULL,
    framework         TEXT NOT NULL,
    framework_version TEXT NOT NULL,
    gpu_type          TEXT NOT NULL,
    launch_cmd        TEXT NOT NULL,
    tp                INTEGER,
    dp                INTEGER,
    pp                INTEGER,
    ep                INTEGER,
    cp                INTEGER,
    kv_cache_dtype    TEXT,
    hicache_enabled   INTEGER,
    flexkv_enabled    INTEGER,
    torch_compile     INTEGER,
    quantization      TEXT,
    attention_backend TEXT,
    extra             TEXT NOT NULL DEFAULT '{}',
    metrics           TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_test_runs_dims
    ON test_runs (model, framework, framework_version, gpu_type);
CREATE INDEX IF NOT EXISTS idx_test_runs_parallel
    ON test_runs (tp, dp, pp, ep, cp);
CREATE TABLE IF NOT EXISTS ingest_log (
    source_dir  TEXT PRIMARY KEY,
    run_id      TEXT,
    ingested_at TEXT NOT NULL
);
"""


def dimension_column(dimension: str) -> str:
    """维度名 → 列名。所有维度都是 test_runs 的直接列；非法维度抛 ValueError。"""
    if dimension not in ALL_DIMENSIONS:
        raise ValueError(f"未知维度: {dimension}")
    return dimension


def doc_to_row(doc: dict) -> dict:
    """入库文档（parser 产出的 dict 形态）→ 表行。"""
    params = doc.get("params", {})
    row = {
        "run_id": doc["_id"],
        "run_timestamp": _iso(doc["run_timestamp"]),
        "model": doc["model"],
        "model_version": doc["model_version"],
        "framework": doc["framework"],
        "framework_version": doc["framework_version"],
        "gpu_type": doc["gpu_type"],
        "launch_cmd": doc["launch_cmd"],
        "extra": json.dumps(doc.get("extra", {}), ensure_ascii=False),
        "metrics": json.dumps(doc.get("metrics", []), ensure_ascii=False),
        "created_at": _iso(doc["created_at"]),
    }
    for dim in PARAM_DIMENSIONS:
        val = params.get(dim)
        if dim in BOOL_PARAMS and val is not None:
            val = 1 if val else 0
        row[dim] = val
    return row


def row_to_doc(row) -> dict:
    """表行（sqlite3.Row）→ 文档形态（下游对齐/工具层统一消费的 dict 结构）。"""
    params = {}
    for dim in PARAM_DIMENSIONS:
        val = row[dim]
        if dim in BOOL_PARAMS and val is not None:
            val = bool(val)
        params[dim] = val
    return {
        "_id": row["run_id"],
        "run_timestamp": datetime.fromisoformat(row["run_timestamp"]),
        "model": row["model"],
        "model_version": row["model_version"],
        "framework": row["framework"],
        "framework_version": row["framework_version"],
        "gpu_type": row["gpu_type"],
        "launch_cmd": row["launch_cmd"],
        "params": params,
        "extra": json.loads(row["extra"]),
        "metrics": json.loads(row["metrics"]),
        "created_at": datetime.fromisoformat(row["created_at"]),
    }


def _iso(dt) -> str:
    return dt.isoformat() if isinstance(dt, datetime) else str(dt)
