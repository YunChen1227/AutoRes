"""
Agent 工具（design.md §7.2）。三个工具，刻意保持最小集：
  - list_dimension_values : 拉取库内某维度的真实取值（归一化对齐 + 澄清列候选）
  - count_matching_runs   : 提交前预检命中数量
  - submit_query_spec     : 工具循环唯一出口，触发报告流水线

工具的 JSON Schema 供 LLM function-calling 使用；实现直接查 SQLite。
"""
from __future__ import annotations

from autores.db import schema
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
                        "description": "可选：按 input_length / concurrency 进一步筛选，值为数组",
                    },
                    "normalize_gpu_scale": {
                        "type": "boolean",
                        "description": (
                            "可选，默认 true：当对比的各配置实际卡数不同时，"
                            "按卡数做弱扩展归一——较少卡的一侧吞吐×(大卡/小卡)、"
                            "并发同比对齐，延迟保持原值，用于'对齐卡数/机器数'的公平对比。"
                            "卡数相同则自动无操作。用户明确要看原始未换算数值时设为 false。"
                        ),
                    },
                },
                "required": ["compare_on"],
            },
        },
    },
]


# ── 工具实现 ──

def summarize_reports(db, filters: dict | None = None) -> dict:
    """按显卡×模型盘点报告数量（忽略框架版本/参数等细节）。"""
    valid_filters = {k: v for k, v in (filters or {}).items()
                     if k in schema.ALL_DIMENSIONS}
    where_sql, params = build_conditions(valid_filters)
    grouped = db.group_counts(["gpu_type", "model"], where_sql, params)

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
    }


def list_dimension_values(db, dimension: str, filters: dict | None = None) -> dict:
    if dimension not in schema.ALL_DIMENSIONS:
        return {"error": f"未知维度: {dimension}", "valid_dimensions": schema.ALL_DIMENSIONS}

    valid_filters = {k: v for k, v in (filters or {}).items()
                     if k in schema.ALL_DIMENSIONS}
    where_sql, params = build_conditions(valid_filters)
    values = db.dimension_values(dimension, where_sql, params)
    return {"dimension": dimension, "values": values}


def count_matching_runs(db, filters: dict, exclude: dict | None = None) -> dict:
    for dim in list(filters or {}) + list(exclude or {}):
        if dim not in schema.ALL_DIMENSIONS:
            return {"error": f"未知维度: {dim}"}

    where_sql, params = build_conditions(filters or {}, exclude)
    count = db.count_runs(where_sql, params)
    result: dict = {"count": count}
    if 0 < count <= 20:
        docs = db.fetch_runs(where_sql, params)
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
