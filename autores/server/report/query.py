"""
QuerySpec → SQLite 查询（design.md §7.3、§7.4 步骤 1-2）。

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
    normalize_gpu_scale: bool = True

    @staticmethod
    def from_dict(d: dict) -> "QuerySpec":
        raw_scale = d.get("normalize_gpu_scale")
        spec = QuerySpec(
            compare_on=d.get("compare_on"),
            filters=d.get("filters") or {},
            compare_values=d.get("compare_values"),
            exclude=d.get("exclude") or {},
            metrics=d.get("metrics"),
            metric_filters=d.get("metric_filters") or {},
            normalize_gpu_scale=True if raw_scale is None else bool(raw_scale),
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


def build_conditions(filters: dict, exclude: dict | None = None) -> tuple[str, list]:
    """
    维度等值/排除条件 → (WHERE 子句, 参数列表)。
    列名全部经 dimension_column 白名单校验，可安全拼接。
    """
    clauses: list[str] = []
    params: list = []

    for dim, val in (filters or {}).items():
        col = schema.dimension_column(dim)
        if isinstance(val, list):
            placeholders = ", ".join("?" for _ in val)
            clauses.append(f"{col} IN ({placeholders})")
            params.extend(val)
        else:
            clauses.append(f"{col} = ?")
            params.append(val)

    for dim, vals in (exclude or {}).items():
        col = schema.dimension_column(dim)
        vals_list = vals if isinstance(vals, list) else [vals]
        placeholders = ", ".join("?" for _ in vals_list)
        # NULL 值不应被排除条件误杀（NOT IN 遇 NULL 恒为 NULL）
        clauses.append(f"({col} NOT IN ({placeholders}) OR {col} IS NULL)")
        params.extend(vals_list)

    return " AND ".join(clauses), params


def build_where(spec: QuerySpec) -> tuple[str, list]:
    """完整 QuerySpec → (WHERE 子句, 参数列表)。"""
    filters = dict(spec.filters)
    where_sql, params = build_conditions(filters, spec.exclude)

    if spec.compare_values:
        col = schema.dimension_column(spec.compare_on)
        placeholders = ", ".join("?" for _ in spec.compare_values)
        clause = f"{col} IN ({placeholders})"
        where_sql = f"{where_sql} AND {clause}" if where_sql else clause
        params.extend(spec.compare_values)

    return where_sql, params


def run_query(db, spec: QuerySpec) -> list[dict]:
    """执行查询，返回匹配的测试记录（文档形态，含 metrics）。"""
    where_sql, params = build_where(spec)
    return db.fetch_runs(where_sql, params)
