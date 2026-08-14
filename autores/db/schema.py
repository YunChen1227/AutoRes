"""
SQLite 表结构定义与共享常量（design.md §6）。

test_runs 表：一次测试 = 一行。
  run_id            : TEXT PRIMARY KEY（= timestamp 目录名，天然唯一/幂等）
  run_timestamp     : TEXT（ISO 8601）
  model / model_version / framework / framework_version / gpu_type / launch_cmd : TEXT
  deployment_mode   : TEXT（'colocated' = 单机/分布式；'pd_disagg' = PD 分离，D22）
  <param>           : 单机/分布式的结构化启动参数（独立列，D18）
  prefill_<param>   : PD 分离时 prefill 实例的同名参数（非 PD 记录为 NULL）
  decode_<param>    : PD 分离时 decode 实例的同名参数（非 PD 记录为 NULL）
  pd_transfer_backend / router_*  : PD 专属（传输后端、路由策略）
  extra             : TEXT（JSON，框架专属细节 + 未识别参数 + PD 原文/长尾字段）
  metrics           : TEXT（JSON 数组，每个 (input_length, concurrency) 组合一条）
  created_at        : TEXT（ISO 8601）

  单行存储约定（D22，用户确认）：
    - 单机/分布式记录：<param> 有值，prefill_/decode_/router_/pd_ 全部 NULL；
    - PD 分离记录：<param> 全部 NULL，prefill_/decode_ 有值，router_/pd_transfer_backend 有值。
    这样"不再为 PD 单独维护一套表结构"，靠 deployment_mode 区分即可。

ingest_log 表：已入库目录台账。
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
# 与 tools/param_map.py 的配对表一一对应（D17）。(名字, SQL 类型, 是否布尔) 三元组，
# 由此同时派生 PARAM_DIMENSIONS / BOOL_PARAMS / DDL 列定义，避免三处手工同步。
#
# 注意几个"看起来能比、实际不能比"的列（详见 param_map.py 各条 note）：
#   mem_fraction / max_running_requests / chunked_prefill_size / ep_enabled 等。
_PARAM_SPECS = [
    # 并行度
    ("tp", "INTEGER", False),
    ("pp", "INTEGER", False),
    ("dp", "INTEGER", False),
    ("dcp", "INTEGER", False),
    ("ep_enabled", "INTEGER", True),
    ("ep_width", "INTEGER", False),
    # 显存 / KV
    ("mem_fraction", "REAL", False),
    ("kv_cache_dtype", "TEXT", False),
    ("page_size", "INTEGER", False),
    ("prefix_caching", "INTEGER", True),
    # 调度
    ("max_running_requests", "INTEGER", False),
    ("chunked_prefill_size", "INTEGER", False),
    ("context_length", "INTEGER", False),
    # 模型 / 量化
    ("dtype", "TEXT", False),
    ("quantization", "TEXT", False),
    ("trust_remote_code", "INTEGER", True),
    ("served_model_name", "TEXT", False),
    # 编译
    ("torch_compile", "INTEGER", True),
    # 后端（sglang 专属，vllm 无等价 CLI flag）
    ("attention_backend", "TEXT", False),
    ("moe_a2a_backend", "TEXT", False),
    ("dp_attention", "INTEGER", True),
    # 投机解码
    ("spec_algorithm", "TEXT", False),
    ("spec_num_steps", "INTEGER", False),
    ("spec_eagle_topk", "INTEGER", False),
    ("spec_num_draft_tokens", "INTEGER", False),
    # KV 分层 / 卸载（两边机制不同，仅粗略近似）
    ("hicache", "INTEGER", True),
]

PARAM_DIMENSIONS = [name for name, _t, _b in _PARAM_SPECS]
_PARAM_SQL_TYPE = {name: t for name, t, _b in _PARAM_SPECS}
_BASE_BOOL = {name for name, _t, b in _PARAM_SPECS if b}

# ── bench（压测）相关维度 ──
#
# 与 launch 参数不同：这些描述的是"压测本身"，会影响结果、必须作为对比轴区分。
#   bench_framework   : 压测工具框架（sglang / vllm）。可与 server 侧 framework 不同
#                       （sglang bench 能打 vllm server，反之亦然，共 4 种组合）。
#   bench_flush_cache : 压测前是否清空 server KV 缓存（bool）。
#                       flush=冷启动无前缀命中；不 flush=复用缓存，两者结果差异大。
#   prefix_rate       : 本次压测共享前缀占输入长度的比例（0~1，REAL）。
#                       真实前缀长度 = round(input_length * prefix_rate)，
#                       与 input_length / concurrency 同为可比对轴。
#                       老数据无此字段 → 默认 0（无前缀）。
# 前三者在入库/上传两条流里都是"必填"，但列本身可空/可缺省，方便老数据迁移。
_BENCH_SPECS = [
    ("bench_framework", "TEXT", False),
    ("bench_flush_cache", "INTEGER", True),
    ("prefix_rate", "REAL", False),
]
BENCH_DIMENSIONS = [name for name, _t, _b in _BENCH_SPECS]
_BENCH_SQL_TYPE = {name: t for name, t, _b in _BENCH_SPECS}
_BENCH_BOOL = {name for name, _t, b in _BENCH_SPECS if b}

# ── PD 分离（D22）──
DEPLOYMENT_MODES = ("colocated", "pd_disagg")
PD_ROLES = ("prefill", "decode")

# prefill_/decode_ 前缀维度（列存在、可存储/展示；暂不进 Agent 对比枚举，见 ALL_DIMENSIONS）
PREFILL_DIMENSIONS = [f"prefill_{n}" for n in PARAM_DIMENSIONS]
DECODE_DIMENSIONS = [f"decode_{n}" for n in PARAM_DIMENSIONS]

# PD 专属独立列（非参数配对，单独定义）
_PD_META_COLUMNS = [
    ("gpu_count", "INTEGER"),
    ("prefill_gpu_count", "INTEGER"),
    ("decode_gpu_count", "INTEGER"),
    ("pd_transfer_backend", "TEXT"),
    ("router_policy", "TEXT"),
    ("router_prefill_policy", "TEXT"),
    ("router_decode_policy", "TEXT"),
]
PD_META_DIMENSIONS = [name for name, _t in _PD_META_COLUMNS]

# 布尔型参数（SQLite 存 0/1，读出还原 bool）——含前缀版本，便于两处判定复用
BOOL_PARAMS = set(_BASE_BOOL)
for _role in PD_ROLES:
    for _n in _BASE_BOOL:
        BOOL_PARAMS.add(f"{_role}_{_n}")


def _base_dim(dim: str) -> str:
    """去掉 prefill_/decode_ 前缀，得到基础参数名（用于查类型/是否布尔）。"""
    for role in PD_ROLES:
        pfx = f"{role}_"
        if dim.startswith(pfx):
            return dim[len(pfx):]
    return dim


def is_bool_dim(dim: str) -> bool:
    base = _base_dim(dim)
    return base in _BASE_BOOL or base in _BENCH_BOOL


# Agent 可用于对比/筛选的维度（design.md §7.2 list_dimension_values 枚举）。
# 加入 deployment_mode 作为"单机分布式 vs PD"的对比轴；prefill_/decode_/router_ 明细
# 已作为物理列存储与展示，如需让 Agent 直接对比可后续追加到此列表。
ALL_DIMENSIONS = (META_DIMENSIONS + PARAM_DIMENSIONS
                  + ["deployment_mode"] + BENCH_DIMENSIONS)

# 指标里的两个"维度列"（不是性能数值，而是测试条件）
METRIC_DIMENSION_KEYS = ["Input_Length", "Concurrency"]

# ── metadata.json 顶层字段 ↔ test_runs 直接列（to_csv / upload / parser 共用）──
#
# 仅列「用户/脚本提供、入库为 test_runs 直接列」的键；params/extra/metrics 等
# 由 launch_cmd 解析或 bench 输出派生，不在此清单。
# CLI 名 = 字段名把 _ 换成 -（argparse 惯例），如 model_version → --model-version。
METADATA_DIRECT_FIELDS: tuple[str, ...] = (
    "model",
    "model_version",       # DB NOT NULL，但允许空串；upload 不要求用户填写
    "framework",           # server（推理服务）框架
    "framework_version",
    "gpu_type",
    "launch_cmd",
    "deployment_mode",     # 默认 colocated
    "bench_framework",
    "bench_flush_cache",
    "prefix_rate",         # 共享前缀占比（0~1）；老数据缺省 0
)

# to_csv.py / 上传入库时用户必须提供的 metadata 字段（model_version 可缺省为空串）
# Scanner 读老 NAS 目录时不强制 bench_*（缺了入库为 NULL）。
METADATA_REQUIRED: frozenset[str] = frozenset({
    "model",
    "framework",
    "framework_version",
    "gpu_type",
    "launch_cmd",
    "bench_framework",
    "bench_flush_cache",
})

# to_csv.py 可选 metadata 字段 → 默认值
# prefix_rate 在 to_csv/上传两条流里都强制必填（各自单独校验），此处的默认值只用于
# 「老目录 metadata.json 缺该字段时」的兜底（persist.build_metadata / parser 读取），
# 与用户要求「旧数据默认 0」一致。
METADATA_OPTIONAL_DEFAULTS: dict[str, object] = {
    "model_version": "",
    "deployment_mode": "colocated",
    "prefix_rate": 0.0,
}

# server / bench 框架 CLI 可选值（与 launch_params.supported_frameworks 对齐）
FRAMEWORK_CHOICES: tuple[str, ...] = ("sglang", "vllm", "vllm-ascend")


# ── test_runs 列清单（DDL 与迁移共用同一份定义）──
def _test_runs_columns() -> list[tuple[str, str]]:
    """返回 [(列名, 完整 SQL 类型定义)]，顺序即建表顺序。"""
    cols: list[tuple[str, str]] = [
        ("run_id", "TEXT PRIMARY KEY"),
        ("run_timestamp", "TEXT NOT NULL"),
        ("model", "TEXT NOT NULL"),
        ("model_version", "TEXT NOT NULL"),
        ("framework", "TEXT NOT NULL"),
        ("framework_version", "TEXT NOT NULL"),
        ("gpu_type", "TEXT NOT NULL"),
        ("launch_cmd", "TEXT NOT NULL"),
        ("deployment_mode", "TEXT NOT NULL DEFAULT 'colocated'"),
    ]
    # bench 维度列（可空：老数据迁移后为 NULL；新数据在 ingest 层强制必填）
    for name in BENCH_DIMENSIONS:
        cols.append((name, _BENCH_SQL_TYPE[name]))
    # 单机/分布式参数列
    for name in PARAM_DIMENSIONS:
        cols.append((name, _PARAM_SQL_TYPE[name]))
    # PD：prefill_/decode_ 前缀列（沿用基础类型，可空）
    for role in PD_ROLES:
        for name in PARAM_DIMENSIONS:
            cols.append((f"{role}_{name}", _PARAM_SQL_TYPE[name]))
    # PD 专属独立列
    cols.extend(_PD_META_COLUMNS)
    # 尾部
    cols.extend([
        ("extra", "TEXT NOT NULL DEFAULT '{}'"),
        ("metrics", "TEXT NOT NULL"),
        ("created_at", "TEXT NOT NULL"),
    ])
    return cols


TEST_RUNS_COLUMNS = _test_runs_columns()
TEST_RUNS_COLUMN_NAMES = [c for c, _ in TEST_RUNS_COLUMNS]

_COLUMN_DEFS = ",\n    ".join(f"{name} {sqltype}" for name, sqltype in TEST_RUNS_COLUMNS)

# 建表与建索引分开：老库已存在时 CREATE TABLE 会跳过，须先 migrate 补列再建索引。
DDL_TABLES = f"""
CREATE TABLE IF NOT EXISTS test_runs (
    {_COLUMN_DEFS}
);
CREATE TABLE IF NOT EXISTS ingest_log (
    source_dir  TEXT PRIMARY KEY,
    run_id      TEXT,
    ingested_at TEXT NOT NULL
);
"""

DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_test_runs_dims
    ON test_runs (model, framework, framework_version, gpu_type);
CREATE INDEX IF NOT EXISTS idx_test_runs_parallel
    ON test_runs (tp, pp, dp, dcp, ep_enabled);
CREATE INDEX IF NOT EXISTS idx_test_runs_deployment
    ON test_runs (deployment_mode);
"""

DDL = DDL_TABLES + DDL_INDEXES


def migrate(conn) -> None:
    """
    对已存在的 test_runs 表补齐缺失列（新增 PD 相关列时用）。
    仅做 ADD COLUMN（可空或带默认值），不改动/删除既有列，安全幂等。
    """
    existing = {r[1] for r in conn.execute("PRAGMA table_info(test_runs)").fetchall()}
    if not existing:
        return  # 表还不存在，DDL 会负责创建
    for name, sqltype in TEST_RUNS_COLUMNS:
        if name in existing:
            continue
        # 迁移时不能带 PRIMARY KEY / 无默认的 NOT NULL；这些列都是后加的可空/带默认列
        add_type = sqltype
        if "PRIMARY KEY" in add_type:
            continue
        conn.execute(f"ALTER TABLE test_runs ADD COLUMN {name} {add_type}")


def dimension_column(dimension: str) -> str:
    """维度名 → 列名。所有维度都是 test_runs 的直接列；非法维度抛 ValueError。"""
    if dimension not in ALL_DIMENSIONS:
        raise ValueError(f"未知维度: {dimension}")
    return dimension


def _store_val(dim: str, val):
    """写库前的值归一：None 保持；布尔参数转 0/1。dim 可带前缀。"""
    if val is None:
        return None
    if is_bool_dim(dim):
        return 1 if val else 0
    return val


def _load_val(dim: str, val):
    """读库后的值还原：布尔参数由 0/1 还原为 bool。dim 可带前缀。"""
    if val is not None and is_bool_dim(dim):
        return bool(val)
    return val


def _rget(row, key, default=None):
    """兼容尚未迁移出该列的 Row（正常迁移后所有列都在）。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def doc_to_row(doc: dict) -> dict:
    """入库文档（parser 产出的 dict 形态）→ 表行。"""
    deployment = doc.get("deployment_mode", "colocated")
    params = doc.get("params") or {}
    pd = doc.get("pd") or {}
    pf_params = (pd.get("prefill") or {}).get("params") or {}
    dc_params = (pd.get("decode") or {}).get("params") or {}
    router = pd.get("router") or {}

    extra_raw = doc.get("extra") or {}
    row = {
        "run_id": doc["_id"],
        "run_timestamp": _iso(doc["run_timestamp"]),
        "model": doc["model"],
        "model_version": doc["model_version"],
        "framework": doc["framework"],
        "framework_version": doc["framework_version"],
        "gpu_type": doc["gpu_type"],
        "launch_cmd": doc["launch_cmd"],
        "deployment_mode": deployment,
        "bench_framework": doc.get("bench_framework"),
        "bench_flush_cache": _store_val("bench_flush_cache", doc.get("bench_flush_cache")),
        # prefix_rate 为 REAL，非布尔；老数据缺失记 0（无前缀）
        "prefix_rate": doc.get("prefix_rate") if doc.get("prefix_rate") is not None else 0,
        "gpu_count": doc.get("gpu_count") or extra_raw.get("gpu_count"),
        "prefill_gpu_count": doc.get("prefill_gpu_count"),
        "decode_gpu_count": doc.get("decode_gpu_count"),
        "pd_transfer_backend": pd.get("transfer_backend"),
        "router_policy": router.get("policy"),
        "router_prefill_policy": router.get("prefill_policy"),
        "router_decode_policy": router.get("decode_policy"),
        "extra": json.dumps(doc.get("extra", {}), ensure_ascii=False),
        "metrics": json.dumps(doc.get("metrics", []), ensure_ascii=False),
        "created_at": _iso(doc["created_at"]),
    }
    for dim in PARAM_DIMENSIONS:
        row[dim] = _store_val(dim, params.get(dim))
        row[f"prefill_{dim}"] = _store_val(dim, pf_params.get(dim))
        row[f"decode_{dim}"] = _store_val(dim, dc_params.get(dim))
    return row


def row_to_doc(row) -> dict:
    """表行（sqlite3.Row）→ 文档形态（下游对齐/工具层统一消费的 dict 结构）。"""
    deployment = _rget(row, "deployment_mode", "colocated") or "colocated"
    params = {dim: _load_val(dim, row[dim]) for dim in PARAM_DIMENSIONS}
    extra = json.loads(row["extra"]) if _rget(row, "extra") else {}

    doc = {
        "_id": row["run_id"],
        "run_timestamp": datetime.fromisoformat(row["run_timestamp"]),
        "model": row["model"],
        "model_version": row["model_version"],
        "framework": row["framework"],
        "framework_version": row["framework_version"],
        "gpu_type": row["gpu_type"],
        "launch_cmd": row["launch_cmd"],
        "deployment_mode": deployment,
        "bench_framework": _rget(row, "bench_framework"),
        "bench_flush_cache": _load_val("bench_flush_cache", _rget(row, "bench_flush_cache")),
        # 迁移后老行该列为 NULL → 读出统一还原为 0（无前缀）
        "prefix_rate": _rget(row, "prefix_rate") if _rget(row, "prefix_rate") is not None else 0,
        "gpu_count": _rget(row, "gpu_count"),
        "prefill_gpu_count": _rget(row, "prefill_gpu_count"),
        "decode_gpu_count": _rget(row, "decode_gpu_count"),
        "params": params,
        "extra": extra,
        "metrics": json.loads(row["metrics"]),
        "created_at": datetime.fromisoformat(row["created_at"]),
    }

    if deployment == "pd_disagg":
        raw_pd = extra.get("pd", {}) if isinstance(extra, dict) else {}
        pf_params = {dim: _load_val(dim, _rget(row, f"prefill_{dim}")) for dim in PARAM_DIMENSIONS}
        dc_params = {dim: _load_val(dim, _rget(row, f"decode_{dim}")) for dim in PARAM_DIMENSIONS}
        doc["pd"] = {
            "transfer_backend": _rget(row, "pd_transfer_backend"),
            "prefill": {"params": pf_params, **(raw_pd.get("prefill") or {})},
            "decode": {"params": dc_params, **(raw_pd.get("decode") or {})},
            "router": {
                "policy": _rget(row, "router_policy"),
                "prefill_policy": _rget(row, "router_prefill_policy"),
                "decode_policy": _rget(row, "router_decode_policy"),
                **(raw_pd.get("router") or {}),
            },
        }
    return doc


def _iso(dt) -> str:
    return dt.isoformat() if isinstance(dt, datetime) else str(dt)
