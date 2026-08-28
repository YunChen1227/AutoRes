"""
Agent 工具（design.md §7.2）：
  - summarize_reports      : 按显卡×模型盘点库内测试记录数量
  - list_dimension_values  : 拉取库内某维度的真实取值（归一化对齐 + 澄清列候选）
  - count_matching_runs    : 提交前预检命中数量
  - analyze_saturation     : 性能饱和点 / hardware wall 分析（JSON + Markdown）
  - submit_query_spec      : 触发生成 Excel 对比报告（对比任务的出口）

工具的 JSON Schema 供 LLM function-calling 使用；实现直接查 SQLite / 确定性分析。
"""
from __future__ import annotations

from autores.db import schema
from autores.server.analysis.saturation import analyze_saturation_runs
from autores.server.report.query import (
    QuerySpec,
    QuerySpecError,
    build_conditions,
)

# ── 工具的 OpenAI function-calling 定义 ──

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "summarize_reports",
            "description": (
                "统计库内报告（测试记录）数量，按显卡×模型汇总。"
                "用于回答'我现在有多少报告''每张卡每个模型各有多少'这类盘点问题。"
                "忽略框架版本、启动参数等细节，只按 gpu_type + model 归并计数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "object",
                        "description": "可选：额外维度等值约束，缩小盘点范围",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dimension_values",
            "description": (
                "列出数据库中某个维度的所有真实取值及各值的记录数。"
                "在把用户口语（如 '4090'）对齐到库内真实值（如 'NVIDIA RTX 4090'）之前，"
                "必须先用本工具确认库内实际有哪些值。也用于向用户列出候选项做澄清。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension": {
                        "type": "string",
                        "enum": schema.ALL_DIMENSIONS,
                        "description": "要查询的维度名",
                    },
                    "filters": {
                        "type": "object",
                        "description": "可选：其他维度的等值约束，缩小统计范围",
                    },
                },
                "required": ["dimension"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_matching_runs",
            "description": (
                "统计满足一组维度条件的测试记录数量（提交 QuerySpec 前必须先预检）。"
                "0 条→告知用户没有该数据；数量过多→提示用户加约束或排除某些取值。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "object",
                        "description": "维度等值条件，值可为数组（表示多选）",
                    },
                    "exclude": {
                        "type": "object",
                        "description": "可选：排除项，键为维度名、值为要排除的取值数组",
                    },
                },
                "required": ["filters"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_query_spec",
            "description": (
                "提交最终 QuerySpec，触发生成 Excel 对比报告。这是完成任务的唯一出口。"
                "提交前须已用 count_matching_runs 预检、且已消除歧义。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "compare_on": {
                        "type": "string",
                        "enum": schema.ALL_DIMENSIONS,
                        "description": "对比轴：在哪个维度上横向比较",
                    },
                    "filters": {
                        "type": "object",
                        "description": "约束项：其余维度保持一致的等值条件",
                    },
                    "compare_values": {
                        "type": "array",
                        "description": "可选：对比轴上的目标取值；缺省=该轴下所有匹配值",
                    },
                    "exclude": {
                        "type": "object",
                        "description": "可选：排除项，键为维度、值为要剔除的取值数组",
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：要对比的指标列名；缺省=全部指标",
                    },
                    "metric_filters": {
                        "type": "object",
                        "description": (
                            "可选：按 metric 行键进一步筛选（text: input_length/"
                            "concurrency/prefix_rate；vlm: 另含 image_count/"
                            "video_count/image_resolution），值为数组"
                        ),
                    },
                    "normalize_gpu_scale": {
                        "type": "boolean",
                        "description": (
                            "可选，默认 true：对比配置规模不同时做弱扩展归一（吞吐×比例、"
                            "并发同比对齐，延迟保持原值）。同 gpu_type 按总卡数；"
                            "多种 gpu_type 混比时优先按机器数（不能整除单机卡数则回退按卡）。"
                            "用户明确要看原始未换算数值时设为 false。"
                        ),
                    },
                },
                "required": ["compare_on"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_saturation",
            "description": (
                "分析性能饱和点（hardware wall）：对匹配的压测记录按 input_length 给出"
                "墙并发、推荐运行点、瓶颈归因与置信度。用于回答'饱和并发多少''性能墙'"
                "'推荐并发''膝点'等容量规划问题。禁止凭肉眼扫指标表估墙，必须用本工具。"
                "命中超过 max_runs（默认 5）时会要求加约束；结果含 markdown 摘要与 caveats。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "object",
                        "description": (
                            "维度等值条件，键必须取自可用维度列表"
                            f"（{', '.join(schema.ALL_DIMENSIONS)}）；值可为数组（多选）"
                        ),
                    },
                    "exclude": {
                        "type": "object",
                        "description": "可选：排除项，键为维度名、值为要排除的取值数组",
                    },
                    "run_id": {
                        "type": "string",
                        "description": "可选：精确指定一条 test_runs.run_id",
                    },
                    "slo_ttft_p99": {
                        "type": "number",
                        "description": "可选：TTFT P99 SLO 上限（ms）",
                    },
                    "slo_tpot_mean": {
                        "type": "number",
                        "description": "可选：TPOT Mean SLO 上限（ms）",
                    },
                    "slo_itl_p95": {
                        "type": "number",
                        "description": "可选：ITL P95 SLO 上限（ms）",
                    },
                    "slo_e2e_p99": {
                        "type": "number",
                        "description": "可选：E2E P99 SLO 上限（ms）",
                    },
                    "plateau_gain": {
                        "type": "number",
                        "description": "可选：吞吐边际增益平台阈值，默认 0.10",
                    },
                    "latency_factor": {
                        "type": "number",
                        "description": "可选：延迟相对基线膝点倍数，默认 2.0",
                    },
                    "headroom": {
                        "type": "number",
                        "description": "可选：推荐运行点 = wall × headroom，默认 0.8",
                    },
                    "include_points": {
                        "type": "boolean",
                        "description": (
                            "可选，默认 false：是否附带逐并发点明细（会显著增大上下文）；"
                            "通常汇总表已够用"
                        ),
                    },
                    "max_runs": {
                        "type": "integer",
                        "description": "可选：最多分析几条 run，默认 5；超出则要求加约束",
                    },
                },
            },
        },
    },
]


# ── 工具实现 ──

_BK_PROP = {
    "type": "string",
    "enum": list(schema.BENCH_KIND_CHOICES),
    "description": "压测类型 text|vlm，默认 text；决定查哪张表与行键集合",
}


def _inject_benchmark_kind_into_defs() -> None:
    """给每个带 parameters.properties 的工具补上 benchmark_kind。"""
    for tool in TOOL_DEFINITIONS:
        props = tool["function"]["parameters"].setdefault("properties", {})
        props.setdefault("benchmark_kind", _BK_PROP)


_inject_benchmark_kind_into_defs()


def summarize_reports(db, filters: dict | None = None,
                      benchmark_kind: str | None = None) -> dict:
    """按显卡×模型盘点报告数量（忽略框架版本/参数等细节）。"""
    kind = schema.resolve_kind(benchmark_kind).name
    valid_filters = {k: v for k, v in (filters or {}).items()
                     if k in schema.ALL_DIMENSIONS}
    where_sql, params = build_conditions(valid_filters)
    grouped = db.group_counts(["gpu_type", "model"], where_sql, params, kind=kind)

    by_gpu: dict = {}
    for row in grouped:
        gpu = row["gpu_type"]
        entry = by_gpu.setdefault(gpu, {"gpu_type": gpu, "models": [], "total": 0})
        entry["models"].append({"model": row["model"], "count": row["count"]})
        entry["total"] += row["count"]

    gpus = sorted(by_gpu.values(), key=lambda e: e["total"], reverse=True)
    return {
        "total_reports": sum(e["total"] for e in gpus),
        "by_gpu": gpus,
        "benchmark_kind": kind,
    }


def list_dimension_values(db, dimension: str, filters: dict | None = None,
                          benchmark_kind: str | None = None) -> dict:
    kind = schema.resolve_kind(benchmark_kind).name
    # dimension=None / 特殊值 → 返回两组维度
    if dimension in (None, "", "all", "*"):
        return {
            "benchmark_kind": kind,
            "run_dimensions": list(schema.ALL_DIMENSIONS),
            "metric_dimensions": list(schema.metric_dims(kind)),
        }
    if dimension not in schema.ALL_DIMENSIONS:
        return {
            "error": f"未知维度: {dimension}",
            "valid_dimensions": list(schema.ALL_DIMENSIONS),
            "metric_dimensions": list(schema.metric_dims(kind)),
            "benchmark_kind": kind,
        }

    valid_filters = {k: v for k, v in (filters or {}).items()
                     if k in schema.ALL_DIMENSIONS}
    where_sql, params = build_conditions(valid_filters)
    values = db.dimension_values(dimension, where_sql, params, kind=kind)
    return {"dimension": dimension, "values": values, "benchmark_kind": kind}


def count_matching_runs(db, filters: dict, exclude: dict | None = None,
                        benchmark_kind: str | None = None) -> dict:
    kind = schema.resolve_kind(benchmark_kind).name
    for dim in list(filters or {}) + list(exclude or {}):
        if dim not in schema.ALL_DIMENSIONS:
            return {"error": f"未知维度: {dim}", "benchmark_kind": kind}

    where_sql, params = build_conditions(filters or {}, exclude)
    count = db.count_runs(where_sql, params, kind=kind)
    result: dict = {"count": count, "benchmark_kind": kind}
    if 0 < count <= 20:
        docs = db.fetch_runs(where_sql, params, kind=kind)
        result["runs"] = [
            {
                "_id": d["_id"],
                "run_timestamp": str(d.get("run_timestamp")),
                "model": d.get("model"),
                "framework": d.get("framework"),
                "framework_version": d.get("framework_version"),
                "gpu_type": d.get("gpu_type"),
                "params": d.get("params"),
            }
            for d in docs
        ]
    return result


def validate_query_spec(spec_dict: dict) -> tuple[QuerySpec | None, str | None]:
    """校验 submit_query_spec 的入参。返回 (QuerySpec, None) 或 (None, 错误信息)。"""
    try:
        return QuerySpec.from_dict(spec_dict), None
    except QuerySpecError as e:
        return None, str(e)


def analyze_saturation(db, args: dict | None = None) -> dict:
    """性能饱和点分析；args 为 LLM/MCP 传入的参数字典。"""
    args = args or {}
    slo = {
        "ttft_p99": args.get("slo_ttft_p99"),
        "tpot_mean": args.get("slo_tpot_mean"),
        "itl_p95": args.get("slo_itl_p95"),
        "e2e_p99": args.get("slo_e2e_p99"),
    }
    kwargs: dict = {
        "filters": args.get("filters"),
        "exclude": args.get("exclude"),
        "run_id": args.get("run_id") or None,
        "slo": slo,
        "include_points": bool(args.get("include_points", False)),
        "benchmark_kind": args.get("benchmark_kind"),
    }
    if args.get("plateau_gain") is not None:
        kwargs["plateau_gain"] = float(args["plateau_gain"])
    if args.get("latency_factor") is not None:
        kwargs["latency_factor"] = float(args["latency_factor"])
    if args.get("headroom") is not None:
        kwargs["headroom"] = float(args["headroom"])
    if args.get("max_runs") is not None:
        kwargs["max_runs"] = int(args["max_runs"])
    return analyze_saturation_runs(db, **kwargs)
