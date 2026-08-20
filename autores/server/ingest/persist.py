"""
上传结果落盘到 benchmark_root，目录结构与 Scanner / to_csv.py 一致。

  {benchmark_root}/YYYYMMDD_HHMMSS/
    ├── result.csv
    └── metadata.json   # launch_cmd 写入 JSON，无单独 txt 文件
"""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timedelta, timezone

from autores.db import schema
from autores.server.ingest.csv_columns import canonical_columns

_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}$")
_NA = "N/A"


class PersistError(Exception):
    """落盘失败（目录冲突、磁盘不可写等）。"""


def _metric_key(column: str, kind: str | None = None) -> str:
    dims = set(schema.metric_dimension_keys(kind))
    return column.lower() if column in dims else column


def metrics_to_csv_rows(metrics: list[dict], kind: str | None = None) -> list[dict[str, object]]:
    """把已解析的 metric 记录转为 to_csv.py 规范 CSV 行。"""
    cols = canonical_columns(kind)
    rows: list[dict[str, object]] = []
    for metric in metrics:
        row: dict[str, object] = {}
        for col in cols:
            val = metric.get(_metric_key(col, kind))
            row[col] = _NA if val is None else val
        rows.append(row)
    return rows


def build_metadata(
    meta: dict,
    launch_cmd: str,
    params: dict,
    extra: dict,
    *,
    deployment_mode: str = "colocated",
    pd: dict | None = None,
    benchmark_kind: str | None = None,
) -> dict:
    """组织 metadata.json（顶层键与 schema.METADATA_DIRECT_FIELDS 一致）。"""
    kind = schema.resolve_kind(
        benchmark_kind or meta.get("benchmark_kind")
    ).name
    out: dict = {}
    for field in schema.METADATA_DIRECT_FIELDS:
        if field == "launch_cmd":
            out[field] = launch_cmd
        elif field == "deployment_mode":
            out[field] = deployment_mode
        elif field == "benchmark_kind":
            out[field] = kind
        else:
            default = schema.METADATA_OPTIONAL_DEFAULTS.get(field)
            out[field] = meta.get(field, default)
    out["params"] = params
    out["extra"] = extra
    out["gpu_count"] = extra.get("gpu_count")
    if deployment_mode == "pd_disagg" and pd is not None:
        out["pd"] = pd
        out["gpu_count"] = pd.get("gpu_count", out["gpu_count"])
        out["prefill_gpu_count"] = pd.get("prefill_gpu_count")
        out["decode_gpu_count"] = pd.get("decode_gpu_count")
    return out


def allocate_timestamp_dir(benchmark_root: str, db, when: datetime,
                           kind: str | None = None) -> str:
    """
    分配符合 Scanner dir_pattern 的目录名（YYYYMMDD_HHMMSS）。
    磁盘目录或 run_id 冲突时顺延秒数，最多尝试 120 次。
    """
    candidate = when.astimezone(timezone.utc)
    bk = schema.resolve_kind(kind).name

    for _ in range(120):
        name = candidate.strftime("%Y%m%d_%H%M%S")
        if not _DIR_PATTERN.match(name):
            raise PersistError(f"时间戳目录名非法: {name}")
        dir_path = os.path.join(benchmark_root, name)
        if os.path.exists(dir_path):
            candidate = candidate + timedelta(seconds=1)
            continue
        if db.count_runs("run_id = ?", [name], kind=bk) > 0:
            candidate = candidate + timedelta(seconds=1)
            continue
        return name
    raise PersistError("无法分配时间戳目录（冲突过多），请稍后重试")


def write_run_dir(dir_path: str, metrics: list[dict], metadata: dict,
                  kind: str | None = None) -> None:
    """写入 result.csv + metadata.json。"""
    os.makedirs(dir_path, exist_ok=True)
    bk = schema.resolve_kind(
        kind or metadata.get("benchmark_kind")
    ).name
    cols = list(canonical_columns(bk))

    csv_path = os.path.join(dir_path, "result.csv")
    rows = metrics_to_csv_rows(metrics, bk)
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            writer.writerows(rows)
    except OSError as e:
        raise PersistError(f"写入 result.csv 失败: {csv_path}: {e}") from e

    meta_path = os.path.join(dir_path, "metadata.json")
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise PersistError(f"写入 metadata.json 失败: {meta_path}: {e}") from e


def persist_upload(
    benchmark_root: str,
    db,
    when: datetime,
    meta: dict,
    metrics: list[dict],
    launch_cmd: str,
    params: dict,
    extra: dict,
    *,
    deployment_mode: str = "colocated",
    pd: dict | None = None,
    benchmark_kind: str | None = None,
) -> tuple[str, str]:
    """
    落盘一次上传。返回 (dir_name, dir_path)。
    benchmark_root 不存在时会创建根目录。
    """
    root = os.path.abspath(benchmark_root)
    os.makedirs(root, exist_ok=True)

    kind = schema.resolve_kind(
        benchmark_kind or meta.get("benchmark_kind")
    ).name
    dir_name = allocate_timestamp_dir(root, db, when, kind)
    dir_path = os.path.join(root, dir_name)
    metadata = build_metadata(
        meta, launch_cmd, params, extra,
        deployment_mode=deployment_mode, pd=pd, benchmark_kind=kind,
    )
    write_run_dir(dir_path, metrics, metadata, kind)
    return dir_name, dir_path
