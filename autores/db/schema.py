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
#
# 与 tools/param_map.py 的配对表一一对应（D17）。新增/删除参数时两边同步改，
# 并跑 `python tools/verify_param_map.py` 确认 flag 仍存在于上游源码。
#
# 注意几个"看起来能比、实际不能比"的列（详见 param_map.py 各条 note）：
#   mem_fraction         量纲不同（vllm 含激活值、sglang 不含），仅在两边都显式设置时可并列
#   max_running_requests sglang 默认 None 按 KV 容量推导，vllm 固定 128
#   chunked_prefill_size sglang 默认 None 按显存档位推导，vllm 固定 2048
#   ep_enabled/ep_width  sglang 是宽度、vllm 是开关，必须归一后再比
PARAM_DIMENSIONS = [
    # 并行度
    "tp",
    "pp",
    "dp",
    "dcp",
    "ep_enabled",
    "ep_width",
    # 显存 / KV
    "mem_fraction",
    "kv_cache_dtype",
    "page_size",
    "prefix_caching",
    # 调度
    "max_running_requests",
    "chunked_prefill_size",
    "context_length",
    # 模型 / 量化
    "dtype",
    "quantization",
    "trust_remote_code",
    "served_model_name",
    # 编译
    "torch_compile",
    # 后端（sglang 专属，vllm 无等价 CLI flag）
    "attention_backend",
    "moe_a2a_backend",
    "dp_attention",
    # 投机解码
    "spec_algorithm",
    "spec_num_steps",
    "spec_eagle_topk",
    "spec_num_draft_tokens",
    # KV 分层 / 卸载（两边机制不同，仅粗略近似）
    "hicache",
]

# 布尔型参数列（SQLite 存 0/1，读出时还原为 bool）
BOOL_PARAMS = {
    "ep_enabled",
    "prefix_caching",
    "trust_remote_code",
    "torch_compile",
    "dp_attention",
    "hicache",
}

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
    -- 并行度
    tp                   INTEGER,
    pp                   INTEGER,
    dp                   INTEGER,
    dcp                  INTEGER,
    ep_enabled           INTEGER,
    ep_width             INTEGER,
    -- 显存 / KV
    mem_fraction         REAL,
    kv_cache_dtype       TEXT,
    page_size            INTEGER,
    prefix_caching       INTEGER,
    -- 调度
    max_running_requests INTEGER,
    chunked_prefill_size INTEGER,
    context_length       INTEGER,
    -- 模型 / 量化
    dtype                TEXT,
    quantization         TEXT,
    trust_remote_code    INTEGER,
    served_model_name    TEXT,
    -- 编译
    torch_compile        INTEGER,
    -- 后端
    attention_backend    TEXT,
    moe_a2a_backend      TEXT,
    dp_attention         INTEGER,
    -- 投机解码
    spec_algorithm       TEXT,
    spec_num_steps       INTEGER,
    spec_eagle_topk      INTEGER,
    spec_num_draft_tokens INTEGER,
    -- KV 分层 / 卸载
    hicache              INTEGER,
    extra             TEXT NOT NULL DEFAULT '{}',
    metrics           TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_test_runs_dims
    ON test_runs (model, framework, framework_version, gpu_type);
CREATE INDEX IF NOT EXISTS idx_test_runs_parallel
    ON test_runs (tp, pp, dp, dcp, ep_enabled);
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
