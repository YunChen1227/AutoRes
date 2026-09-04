"""
数据对齐（design.md §7.4 步骤 2-3）。

1. 合并去重：按 run 级维度分组，同组多条 run 的 metrics 按行键取并集；
   同一行键冲突时保留 run_timestamp 更大的。不同场景各占各的行，绝不混算。
2. 透视为宽表：行 = 场景（kind 对应的全部 metric 行键），列 = 对比轴取值。缺失填 N/A。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from autores.db import schema

NA = "N/A"


def _dimension_value(doc: dict, dim: str) -> Any:
    if dim in schema.PARAM_DIMENSIONS:
        return doc.get("params", {}).get(dim)
    return doc.get(dim)


def _group_key(doc: dict) -> tuple:
    """
    合并去重的分组键 = 全部 run 级维度（含 compare_on）的取值组合。
    同一组合的多次测试才算"同组"；compare_on 不同值自然不同组，全保留。
    """
    return tuple((dim, _dimension_value(doc, dim)) for dim in schema.ALL_DIMENSIONS)


def _metric_row_key(metric: dict, kind: str | None = None) -> tuple:
    """一条指标记录的维度键 = kind 对应的全部 metric 行键取值。"""
    return tuple(metric.get(d) for d in schema.metric_dims(kind))


def _metric_dims_dict(metric: dict, kind: str | None = None) -> dict:
    return {d: metric.get(d) for d in schema.metric_dims(kind)}


def merge_duplicates(docs: list[dict], kind: str | None = None) -> list[dict]:
    """
    按 run 级维度分组，同组多条 run 的 metrics 按行键取并集合并成一条。
    同一行键冲突时保留 run_timestamp 更大的那条 metric。
    合并来源记入 _merged_from（run_id 列表）。
    """
    bk = schema.resolve_kind(kind).name
    groups: dict[tuple, list[dict]] = {}
    for doc in docs:
        groups.setdefault(_group_key(doc), []).append(doc)

    merged: list[dict] = []
    for group in groups.values():
        # 按时间戳升序，后写覆盖先写
        group_sorted = sorted(group, key=lambda d: d.get("run_timestamp") or "")
        base = deepcopy(group_sorted[-1])
        by_key: dict[tuple, dict] = {}
        sources: list[str] = []
        for doc in group_sorted:
            rid = doc.get("_id") or doc.get("run_id")
            if rid is not None:
                sources.append(str(rid))
            for metric in doc.get("metrics") or []:
                by_key[_metric_row_key(metric, bk)] = deepcopy(metric)
        base["metrics"] = list(by_key.values())
        if len(sources) > 1:
            base["_merged_from"] = sources
        merged.append(base)
    return merged


# 兼容旧名
def take_latest(docs: list[dict], kind: str | None = None) -> list[dict]:
    return merge_duplicates(docs, kind)


def _passes_metric_filter(metric: dict, metric_filters: dict) -> bool:
    for key, allowed in metric_filters.items():
        if metric.get(key) not in allowed:
            return False
    return True


def _sort_dim_value(dim: str, val: Any) -> tuple:
    """排序键：数值优先；image_resolution 按 H×W 再按字面量；其余字符串按字面量。"""
    if val is None:
        return (2, 0, 0, "")
    if dim == "image_resolution" and isinstance(val, str):
        parts = val.lower().split("x")
        if len(parts) == 2:
            try:
                h, w = int(parts[0]), int(parts[1])
                return (0, h, w, val)
            except ValueError:
                pass
        return (1, 0, 0, val)
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return (0, float(val), 0, "")
    return (1, 0, 0, str(val))


_SHEET_DIM_SHORT = {
    "prefix_rate": "pr", "image_count": "img",
    "video_count": "vid", "image_resolution": "res",
}


def _sheet_sort_key(sheet_dims: list[str], key_tuple: tuple) -> tuple:
    return tuple(_sort_dim_value(d, v) for d, v in zip(sheet_dims, key_tuple))


def _sheet_label(sheet_dims: list[str], key_tuple: tuple) -> str:
    parts = []
    for d, v in zip(sheet_dims, key_tuple):
        short = _SHEET_DIM_SHORT.get(d, d)
        parts.append(f"{short}={'N-A' if v is None else v}")
    return "/".join(parts) if parts else "all"


def split_table_into_sheets(table: dict, sheet_dims: list[str]) -> dict:
    """把扁平对比表按 sheet_dims（如 ['prefix_rate']）拆成多 sheet 视图。

    - sheet 集合 = 各副本在 sheet_dims 上取值的**并集**（缺该取值的副本，其列在
      对应 sheet 里整列 N/A —— 由渲染层的逐单元格 .get(col, N/A) 兜底）。
    - 每个 sheet 内：行 = 剩余行键（text: input_length/concurrency），
      列 = compare_on 各取值（全局一致，跨 sheet 对齐）。
    - 两列都有数据才算差异（沿用 excel 渲染逻辑，一列 N/A → 差异 N/A）。

    返回在原 table 基础上追加 "sheets" / "sheet_dim_keys" / "sheet_dim_labels"；
    原有键（column_labels/metric_names/constraints/notes/rows 等）保持不变，
    供渲染层作全局信息复用。sheet_dims 为空则原样返回（单 sheet 语义）。
    """
    dim_keys = list(table.get("dim_keys") or [])
    dim_labels = list(table.get("dim_labels") or [])
    label_by_key = dict(zip(dim_keys, dim_labels))
    sheet_dims = [d for d in sheet_dims if d in dim_keys]
    if not sheet_dims:
        return table

    remaining_keys = [d for d in dim_keys if d not in sheet_dims]
    remaining_labels = [label_by_key[d] for d in remaining_keys]
    column_labels = table.get("column_labels", [])
    metric_names = table.get("metric_names", [])

    groups: dict[tuple, list[dict]] = {}
    for entry in table.get("matrix", []):
        sk = tuple(entry.get("dims", {}).get(d) for d in sheet_dims)
        groups.setdefault(sk, []).append(entry)

    sheets: list[dict] = []
    for sk in sorted(groups, key=lambda k: _sheet_sort_key(sheet_dims, k)):
        sub_matrix = []
        for e in groups[sk]:
            e_dims = e.get("dims", {})
            sub_matrix.append({
                "dims": {k: e_dims.get(k) for k in remaining_keys},
                "input_length": e_dims.get("input_length"),
                "concurrency": e_dims.get("concurrency"),
                "metrics": e.get("metrics", {}),
            })
        # 对齐率：所有对比列都非 N/A 的场景行数
        total_rows = len(sub_matrix)
        aligned_rows = 0
        for e in sub_matrix:
            fully = bool(metric_names)
            for mname in metric_names:
                per_column = e["metrics"].get(mname, {})
                if any(per_column.get(col, NA) == NA for col in column_labels):
                    fully = False
                    break
            if fully:
                aligned_rows += 1
        sheets.append({
            "sheet_dims": dict(zip(sheet_dims, sk)),
            "sheet_label": _sheet_label(sheet_dims, sk),
            "dim_keys": remaining_keys,
            "dim_labels": remaining_labels,
            "matrix": sub_matrix,
            "coverage": {"total_rows": total_rows, "aligned_rows": aligned_rows},
        })

    out = dict(table)
    out["sheets"] = sheets
    out["sheet_dim_keys"] = sheet_dims
    out["sheet_dim_labels"] = [label_by_key[d] for d in sheet_dims]
    return out


def build_comparison_table(
    docs: list[dict],
    compare_on: str,
    metrics: list[str] | None = None,
    metric_filters: dict | None = None,
    gpu_scaled: bool = False,
    kind: str | None = None,
) -> dict:
    """
    构建对比宽表。返回：
      {
        "column_labels": [...],
        "metric_names": [...],
        "dim_keys": [小写行键名, ...],
        "dim_labels": [CSV 规范列名, ...],
        "matrix": [{"dims": {...}, "metrics": {指标名: {列标签: 值}}}],
        "rows": [...],
        "constraints": {...},
        "notes": {...},
        "coverage": {"total_rows": N, "aligned_rows": M},
      }
    """
    bk = schema.resolve_kind(
        kind or (docs[0].get("benchmark_kind") if docs else None)
    ).name
    dim_keys = list(schema.metric_dims(bk))
    dim_labels = list(schema.metric_dimension_keys(bk))
    metric_filters = metric_filters or {}

    # 列：对比轴的各取值（保持出现顺序、去重）
    column_labels: list = []
    for doc in docs:
        cv = _dimension_value(doc, compare_on)
        if cv not in column_labels:
            column_labels.append(cv)

    # 每列的卡数 / 显卡型号（弱扩展对比时用于表头标注）
    column_gpus: dict = {}
    column_gpu_types: dict = {}
    column_scale: dict = {}
    for doc in docs:
        cv = _dimension_value(doc, compare_on)
        if "_gpus" in doc and cv not in column_gpus:
            column_gpus[cv] = doc.get("_gpus")
            column_gpu_types[cv] = doc.get("gpu_type", "")
            column_scale[cv] = doc.get("_scale", 1)

    # 收集所有 (行键..., metric_name) 作为行；值按列填充
    rows_map: dict[tuple, dict] = {}
    all_metric_names: list[str] = []
    dim_key_set = set(dim_keys)

    for doc in docs:
        col = _dimension_value(doc, compare_on)
        for metric in doc.get("metrics", []):
            if not _passes_metric_filter(metric, metric_filters):
                continue
            row_key = _metric_row_key(metric, bk)
            for mname, mval in metric.items():
                if mname in dim_key_set:
                    continue
                if metrics is not None and mname not in metrics:
                    continue
                if mname not in all_metric_names:
                    all_metric_names.append(mname)
                rk = row_key + (mname,)
                rows_map.setdefault(rk, {})[col] = mval if mval is not None else NA

    def row_sort_key(rk: tuple):
        dim_vals = rk[:-1]
        mname = rk[-1]
        return (
            tuple(_sort_dim_value(d, v) for d, v in zip(dim_keys, dim_vals)),
            all_metric_names.index(mname),
        )

    rows = []
    for rk in sorted(rows_map, key=row_sort_key):
        dim_vals = rk[:-1]
        mname = rk[-1]
        values = {col: rows_map[rk].get(col, NA) for col in column_labels}
        dims = dict(zip(dim_keys, dim_vals))
        label_parts = []
        for d, v in dims.items():
            if v is not None:
                short = {"input_length": "in", "concurrency": "c",
                         "prefix_rate": "pr", "image_count": "img",
                         "video_count": "vid", "image_resolution": "res"}.get(d, d)
                label_parts.append(f"{short}{v}")
        label_prefix = "/".join(label_parts)
        rows.append({
            "metric_key": rk,
            "dims": dims,
            # 兼容旧字段
            "input_length": dims.get("input_length"),
            "concurrency": dims.get("concurrency"),
            "metric_name": mname,
            "label": f"{label_prefix} · {mname}" if label_prefix else mname,
            "values": values,
        })

    # 矩阵视图：行 = 场景行键，每行内按指标名归组
    matrix: list[dict] = []
    matrix_index: dict[tuple, dict] = {}
    for row in rows:
        cond = tuple(row["dims"].get(d) for d in dim_keys)
        entry = matrix_index.get(cond)
        if entry is None:
            entry = {
                "dims": dict(row["dims"]),
                "input_length": row["dims"].get("input_length"),
                "concurrency": row["dims"].get("concurrency"),
                "metrics": {},
            }
            matrix_index[cond] = entry
            matrix.append(entry)
        entry["metrics"][row["metric_name"]] = row["values"]

    # 对齐率：所有对比列都非 N/A 的场景行数
    total_rows = len(matrix)
    aligned_rows = 0
    for entry in matrix:
        fully = True
        for mname in all_metric_names:
            per_column = entry["metrics"].get(mname, {})
            for col in column_labels:
                if per_column.get(col, NA) == NA:
                    fully = False
                    break
            if not fully:
                break
        if fully and all_metric_names:
            aligned_rows += 1

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
    merged_from = []
    for doc in docs:
        if doc.get("_merged_from"):
            merged_from.extend(doc["_merged_from"])
    notes = {
        "multi_framework": len(frameworks) > 1,
        "multi_version": len(versions) > len(frameworks),
        "merged_from": sorted(set(merged_from)) if merged_from else [],
        "benchmark_kind": bk,
    }

    return {
        "column_labels": column_labels,
        "metric_names": all_metric_names,
        "dim_keys": dim_keys,
        "dim_labels": dim_labels,
        "matrix": matrix,
        "rows": rows,
        "constraints": constraints,
        "notes": notes,
        "num_runs": len(docs),
        "gpu_scaled": gpu_scaled and any(s != 1 for s in column_scale.values()),
        "column_gpus": column_gpus,
        "column_gpu_types": column_gpu_types,
        "column_scale": column_scale,
        "coverage": {"total_rows": total_rows, "aligned_rows": aligned_rows},
        "benchmark_kind": bk,
    }
