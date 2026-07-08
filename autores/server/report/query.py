"""
QuerySpec → MongoDB 查询（design.md §7.3、§7.4 步骤 1-2）。

QuerySpec 结构：
{
  "compare_on": "gpu_type",                 # 对比轴（单个维度）
  "filters": {"model": "GLM-4.5", "tp": 8}, # 约束项（等值，值可为数组）
  "compare_values": ["H20-141G", "H800"],   # 可选：对比轴目标值
  "exclude": {"gpu_type": ["H20-96G"]},      # 可选：排除项
  "metrics": ["Output_Throughput"],          # 可选：要对比的指标列
  "metric_filters": {"input_length":[1024], "concurrency":[32]}  # 可选
}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from autores.db import client as dbc
from autores.db import schema


class QuerySpecError(ValueError):
    """QuerySpec 非法（校验失败）。"""


@dataclass
class QuerySpec:
    compare_on: str
    filters: dict[str, Any] = field(default_factory=dict)
    compare_values: list[Any] | None = None
    exclude: dict[str, list[Any]] = field(default_factory=dict)
    metrics: list[str] | None = None
    metric_filters: dict[str, list[Any]] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: dict) -> "QuerySpec":
        spec = QuerySpec(
            compare_on=d.get("compare_on"),
            filters=d.get("filters") or {},
            compare_values=d.get("compare_values"),
            exclude=d.get("exclude") or {},
            metrics=d.get("metrics"),
            metric_filters=d.get("metric_filters") or {},
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        if not self.compare_on:
            raise QuerySpecError("compare_on 不能为空")
        if self.compare_on not in schema.ALL_DIMENSIONS:
            raise QuerySpecError(f"compare_on 非法维度: {self.compare_on}")
        for dim in list(self.filters) + list(self.exclude):
            if dim not in schema.ALL_DIMENSIONS:
                raise QuerySpecError(f"未知维度: {dim}")
        if self.compare_on in self.filters:
            raise QuerySpecError("compare_on 不应同时出现在 filters 中（用 compare_values 指定目标值）")
        # 注意：compare_on 允许出现在 exclude 中——即"在该轴上对比、但排除某些取值"（D21）。


def _eq_or_in(value: Any):
    """标量 → 等值；列表 → $in。"""
    if isinstance(value, list):
        return {"$in": value}
    return value


def build_match(spec: QuerySpec) -> dict:
    """QuerySpec 的维度条件 → Mongo $match 文档。"""
    match: dict[str, Any] = {}

    for dim, val in spec.filters.items():
        match[schema.dimension_field_path(dim)] = _eq_or_in(val)

    if spec.compare_values:
        match[schema.dimension_field_path(spec.compare_on)] = {"$in": spec.compare_values}

    for dim, vals in spec.exclude.items():
        path = schema.dimension_field_path(dim)
        if path in match and isinstance(match[path], dict) and "$in" in match[path]:
            # 同一维度既 in 又 nin：取差集语义，追加 $nin
            match[path]["$nin"] = vals
        else:
            match[path] = {"$nin": vals if isinstance(vals, list) else [vals]}

    return match


def run_query(db, spec: QuerySpec) -> list[dict]:
    """执行查询，返回匹配的 test_runs 文档（含内嵌 metrics）。"""
    match = build_match(spec)
    return list(dbc.test_runs(db).find(match))


def count_query(db, match: dict) -> int:
    return dbc.test_runs(db).count_documents(match)
