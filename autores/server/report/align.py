"""
数据对齐（design.md §7.4 步骤 2-3）。

1. 取最新（D20）：按"除时间戳外所有维度组合"分组，每组取最新一次。
   不同 framework_version、不同 framework 天然落在不同分组，全部保留。
2. 透视为宽表：行 = 指标(含输入长度/并发)，列 = 对比轴取值。缺失填 N/A。
"""
from __future__ import annotations

from typing import Any

from autores.db import schema

NA = "N/A"

# 分组签名用的维度（除 compare_on 外的所有维度）——用于 D20 取最新。
# compare_on 是对比轴，同一轴的不同取值本就该成为不同列，故不参与"是否同一组"判断
# 但要成为不同组以取各自最新，仍需把 compare_on 计入分组键。见下方 _group_key 说明。


def _dimension_value(doc: dict, dim: str) -> Any:
    if dim in schema.PARAM_DIMENSIONS:
        return doc.get("params", {}).get(dim)
    return doc.get(dim)


def _group_key(doc: dict) -> tuple:
    """
    取最新（D20）的分组键 = 全部维度（含 compare_on）的取值组合。
    同一组合的多次测试才算"重复"，取最新；compare_on 不同值自然不同组，全保留。
    """
    return tuple((dim, _dimension_value(doc, dim)) for dim in schema.ALL_DIMENSIONS)


def take_latest(docs: list[dict]) -> list[dict]:
    """按全维度分组，每组保留 run_timestamp 最大的一条。"""
    latest: dict[tuple, dict] = {}
    for doc in docs:
        key = _group_key(doc)
        cur = latest.get(key)
        if cur is None or doc.get("run_timestamp") > cur.get("run_timestamp"):
            latest[key] = doc
    return list(latest.values())


def _metric_row_key(metric: dict) -> tuple:
    """一条指标记录的维度键 = (input_length, concurrency)。"""
    return (metric.get("input_length"), metric.get("concurrency"))


def _passes_metric_filter(metric: dict, metric_filters: dict) -> bool:
    for key, allowed in metric_filters.items():
        if metric.get(key) not in allowed:
            return False
    return True


def build_comparison_table(
    docs: list[dict],
    compare_on: str,
    metrics: list[str] | None = None,
    metric_filters: dict | None = None,
    gpu_scaled: bool = False,
) -> dict:
    """
    构建对比宽表。返回：
      {
        "column_labels": [对比轴取值1, 取值2, ...],   # 对比轴取值（渲染时每个指标组内的子列）
        "metric_names": [指标名, ...],                # 渲染时的列组顺序
        "matrix": [                                   # 渲染主结构：一行一个测试条件组合
           {"input_length": 1024, "concurrency": 32,
            "metrics": {指标名: {列标签: 值 或 "N/A"}}},
           ...
        ],
        "rows": [                                     # 扁平视图（保留给计数/下游消费）
           {"metric_key": (input_len, conc, metric_name),
            "label": "in1024/c32 · Output_Throughput",
            "values": {列标签: 值 或 "N/A"}},
           ...
        ],
        "constraints": {共同约束项展示},
        "notes": {"multi_framework": bool, "multi_version": bool},
      }
    """
    metric_filters = metric_filters or {}

    # 列：对比轴的各取值（保持出现顺序、去重）
    column_labels: list = []
    for doc in docs:
        cv = _dimension_value(doc, compare_on)
        if cv not in column_labels:
            column_labels.append(cv)

    # 每列的卡数换算信息（弱扩展对比时用于表头标注）
    column_gpus: dict = {}
    column_scale: dict = {}
    for doc in docs:
        cv = _dimension_value(doc, compare_on)
        if "_gpus" in doc and cv not in column_gpus:
            column_gpus[cv] = doc.get("_gpus")
            column_scale[cv] = doc.get("_scale", 1)

    # 收集所有 (input_length, concurrency, metric_name) 作为行；值按列填充
    # 结构：rows_map[(il, cc, metric_name)][column_label] = value
    rows_map: dict[tuple, dict] = {}
    all_metric_names: list[str] = []

    for doc in docs:
        col = _dimension_value(doc, compare_on)
        for metric in doc.get("metrics", []):
            if not _passes_metric_filter(metric, metric_filters):
                continue
            il, cc = _metric_row_key(metric)
            for mname, mval in metric.items():
                if mname in ("input_length", "concurrency"):
                    continue
                if metrics is not None and mname not in metrics:
                    continue
                if mname not in all_metric_names:
                    all_metric_names.append(mname)
                rk = (il, cc, mname)
                rows_map.setdefault(rk, {})[col] = mval if mval is not None else NA

    # 排序：先按 input_length、concurrency，再按指标出现顺序
    def row_sort_key(rk):
        il, cc, mname = rk
        il_s = il if isinstance(il, (int, float)) else -1
        cc_s = cc if isinstance(cc, (int, float)) else -1
        return (il_s, cc_s, all_metric_names.index(mname))

    rows = []
    for rk in sorted(rows_map, key=row_sort_key):
        il, cc, mname = rk
        values = {col: rows_map[rk].get(col, NA) for col in column_labels}
        label_prefix = f"in{il}/c{cc}" if il is not None and cc is not None else ""
        rows.append({
            "metric_key": rk,
            "input_length": il,
            "concurrency": cc,
            "metric_name": mname,
            "label": f"{label_prefix} · {mname}" if label_prefix else mname,
            "values": values,
        })

    # 矩阵视图：行 = (input_length, concurrency)，每行内按指标名归组
    # 行序沿用 rows 的排序结果（已按 input_length、concurrency 排好）
    matrix: list[dict] = []
    matrix_index: dict[tuple, dict] = {}
    for row in rows:
        cond = (row["input_length"], row["concurrency"])
        entry = matrix_index.get(cond)
        if entry is None:
            entry = {
                "input_length": row["input_length"],
                "concurrency": row["concurrency"],
                "metrics": {},
            }
            matrix_index[cond] = entry
            matrix.append(entry)
        entry["metrics"][row["metric_name"]] = row["values"]

    # 约束项：所有文档中取值一致的维度（用于报告标题区）
    constraints = {}
    for dim in schema.ALL_DIMENSIONS:
        if dim == compare_on:
            continue
        vals = {_dimension_value(doc, dim) for doc in docs}
        if len(vals) == 1:
            only = next(iter(vals))
            if only is not None:
                constraints[dim] = only

    frameworks = {doc.get("framework") for doc in docs}
    versions = {(doc.get("framework"), doc.get("framework_version")) for doc in docs}
    notes = {
        "multi_framework": len(frameworks) > 1,
        "multi_version": len(versions) > len(frameworks),
    }

    return {
        "column_labels": column_labels,
        "metric_names": all_metric_names,
        "matrix": matrix,
        "rows": rows,
        "constraints": constraints,
        "notes": notes,
        "num_runs": len(docs),
        "gpu_scaled": gpu_scaled and any(s != 1 for s in column_scale.values()),
        "column_gpus": column_gpus,
        "column_scale": column_scale,
    }
