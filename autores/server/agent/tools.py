"""
Agent 工具（design.md §7.2）。三个工具，刻意保持最小集：
  - list_dimension_values : 拉取库内某维度的真实取值（归一化对齐 + 澄清列候选）
  - count_matching_runs   : 提交前预检命中数量
  - submit_query_spec     : 工具循环唯一出口，触发报告流水线

工具的 JSON Schema 供 LLM function-calling 使用；实现直接读 Mongo。
"""
from __future__ import annotations

from typing import Any

from autores.db import client as dbc
from autores.db import schema
from autores.server.report.query import QuerySpec, QuerySpecError, build_match

# ── 工具的 OpenAI function-calling 定义 ──

TOOL_DEFINITIONS = [
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
                },
                "required": ["compare_on"],
            },
        },
    },
]


# ── 工具实现 ──

def list_dimension_values(db, dimension: str, filters: dict | None = None) -> dict:
    if dimension not in schema.ALL_DIMENSIONS:
        return {"error": f"未知维度: {dimension}", "valid_dimensions": schema.ALL_DIMENSIONS}

    field_path = schema.dimension_field_path(dimension)
    match: dict[str, Any] = {}
    if filters:
        for dim, val in filters.items():
            if dim in schema.ALL_DIMENSIONS:
                match[schema.dimension_field_path(dim)] = (
                    {"$in": val} if isinstance(val, list) else val
                )

    pipeline = []
    if match:
        pipeline.append({"$match": match})
    pipeline.append({"$group": {"_id": f"${field_path}", "count": {"$sum": 1}}})
    pipeline.append({"$sort": {"count": -1}})

    results = list(dbc.test_runs(db).aggregate(pipeline))
    values = [{"value": r["_id"], "count": r["count"]} for r in results]
    return {"dimension": dimension, "values": values}


def count_matching_runs(db, filters: dict, exclude: dict | None = None) -> dict:
    match: dict[str, Any] = {}
    for dim, val in (filters or {}).items():
        if dim not in schema.ALL_DIMENSIONS:
            return {"error": f"未知维度: {dim}"}
        match[schema.dimension_field_path(dim)] = (
            {"$in": val} if isinstance(val, list) else val
        )
    for dim, vals in (exclude or {}).items():
        if dim not in schema.ALL_DIMENSIONS:
            return {"error": f"未知维度: {dim}"}
        path = schema.dimension_field_path(dim)
        vals_list = vals if isinstance(vals, list) else [vals]
        if path in match and isinstance(match[path], dict):
            match[path]["$nin"] = vals_list
        else:
            match[path] = {"$nin": vals_list}

    count = dbc.test_runs(db).count_documents(match)
    result: dict[str, Any] = {"count": count}
    if 0 < count <= 20:
        docs = dbc.test_runs(db).find(match, {"metrics": 0, "extra": 0}).limit(20)
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
