"""
MongoDB 文档结构定义与共享常量（design.md §6）。

test_runs 文档结构：
  _id                : str  (= timestamp 目录名，天然唯一/幂等)
  run_timestamp      : datetime
  model, model_version, framework, framework_version, gpu_type, launch_cmd : str
  params             : dict  (tp/dp/pp/ep/cp/kv_cache_dtype/hicache_enabled/...)
  extra              : dict  (框架专属细节 + unrecognized)
  metrics            : list[dict]  (每个 (input_length, concurrency) 组合一条)
  created_at         : datetime

ingest_log 文档结构：
  _id                : str  (= timestamp 目录名)
  ingested_at        : datetime
"""
from __future__ import annotations

COLLECTION_TEST_RUNS = "test_runs"
COLLECTION_INGEST_LOG = "ingest_log"

# ── 元信息维度（test_runs 顶层字段）──
META_DIMENSIONS = [
    "model",
    "model_version",
    "framework",
    "framework_version",
    "gpu_type",
]

# ── 结构化启动参数维度（params 子对象里的键）──
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

# Agent 可用于对比/筛选的全部维度（design.md §7.2 list_dimension_values 枚举）
ALL_DIMENSIONS = META_DIMENSIONS + PARAM_DIMENSIONS

# 指标里的两个"维度列"（不是性能数值，而是测试条件）
METRIC_DIMENSION_KEYS = ["Input_Length", "Concurrency"]


def dimension_field_path(dimension: str) -> str:
    """把维度名映射到 Mongo 文档里的字段路径。params 维度需加 'params.' 前缀。"""
    if dimension in PARAM_DIMENSIONS:
        return f"params.{dimension}"
    return dimension


def build_indexes(db) -> None:
    """确保集合索引存在（幂等，两进程启动时各自调用）。"""
    runs = db[COLLECTION_TEST_RUNS]
    runs.create_index(
        [("model", 1), ("framework", 1), ("framework_version", 1), ("gpu_type", 1)],
        name="dims",
    )
    runs.create_index(
        [("params.tp", 1), ("params.dp", 1), ("params.pp", 1),
         ("params.ep", 1), ("params.cp", 1)],
        name="parallel",
    )
    # _id 天然唯一，无需额外唯一索引
