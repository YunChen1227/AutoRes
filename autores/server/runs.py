"""
测试记录管理服务层（D26）。

删除范围：仅页面上传（extra.ingest_source == "manual_upload"）的记录。
删除动作：磁盘目录 → runs 表行 + ingest_log 台账（先目录后库，崩溃可重试）。
MCP / Agent 刻意不暴露本模块的删除能力。
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Any

from autores.db import schema

_MANUAL_UPLOAD = "manual_upload"
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 2000


class RunDeleteError(Exception):
    """删除/预览非法；上层转成 400。"""


def resolve_kind_of(db, run_id: str, kind: str | None = None) -> str:
    """
    解析记录所属 benchmark_kind。
    kind 已给出则直接校验；否则依次查 text / vlm（同名 run_id 不可能跨表）。
    """
    if kind:
        bk = schema.resolve_kind(kind).name
        if db.count_runs("run_id = ?", [run_id], kind=bk) == 0:
            raise RunDeleteError(f"记录不存在: {run_id}（kind={bk}）")
        return bk
    for candidate in schema.BENCH_KIND_CHOICES:
        if db.count_runs("run_id = ?", [run_id], kind=candidate) > 0:
            return candidate
    raise RunDeleteError(f"记录不存在: {run_id}")


def _source_dir_of(db, run_id: str) -> str:
    """优先 ingest_log.source_dir；无台账时用 run_id（上传流里二者相等）。"""
    entry = db.ingest_log_entry(run_id)
    if entry and entry.get("source_dir"):
        return str(entry["source_dir"])
    return run_id


def _disk_path(benchmark_root: str, source_dir: str) -> str:
    return os.path.join(os.path.abspath(benchmark_root), source_dir)


def _guard_path(
    source_dir: str,
    *,
    benchmark_root: str,
    dir_pattern: str,
    require_metadata_if_exists: bool = True,
) -> tuple[str, bool]:
    """
    路径护栏。返回 (disk_path, disk_exists)。
    disk_exists=False 时允许继续（崩溃重试路径：目录已没、库还在）。
    """
    if not source_dir or not isinstance(source_dir, str):
        raise RunDeleteError("source_dir 为空")
    if os.path.sep in source_dir or (os.path.altsep and os.path.altsep in source_dir):
        raise RunDeleteError(f"source_dir 非法（含路径分隔符）: {source_dir!r}")
    if ".." in source_dir or source_dir.startswith(("/", "\\")):
        raise RunDeleteError(f"source_dir 非法: {source_dir!r}")
    try:
        rx = re.compile(dir_pattern)
    except re.error as e:
        raise RunDeleteError(f"dir_pattern 非法: {e}") from e
    if not rx.match(source_dir):
        raise RunDeleteError(
            f"source_dir 不匹配目录格式 {dir_pattern!r}: {source_dir!r}")

    root_real = os.path.realpath(os.path.abspath(benchmark_root))
    dir_path = os.path.realpath(_disk_path(benchmark_root, source_dir))
    if os.path.dirname(dir_path) != root_real:
        raise RunDeleteError(
            f"路径逃逸拒绝：{dir_path} 不在 {root_real} 下一层")

    exists = os.path.isdir(dir_path)
    if exists and require_metadata_if_exists:
        meta = os.path.join(dir_path, "metadata.json")
        if not os.path.isfile(meta):
            raise RunDeleteError(
                f"目录存在但不含 metadata.json，拒绝递归删除: {dir_path}")
    return dir_path, exists


def _load_doc(db, run_id: str, kind: str) -> dict:
    docs = db.fetch_runs("run_id = ?", [run_id], kind=kind)
    if not docs:
        raise RunDeleteError(f"记录不存在: {run_id}（kind={kind}）")
    return docs[0]


def _assert_manual_upload(doc: dict) -> None:
    extra = doc.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}
    src = extra.get("ingest_source")
    if src != _MANUAL_UPLOAD:
        raise RunDeleteError(
            "仅允许删除页面上传（manual_upload）的记录；"
            "Scanner 从 NAS 扫入的原始产物不可通过本接口删除。"
            + (f"（当前 ingest_source={src!r}）" if src else "（无 ingest_source 标记）")
        )


def _annotate_brief(db, brief: dict, benchmark_root: str | None) -> dict:
    extra = brief.get("extra") or {}
    ingest_source = extra.get("ingest_source") if isinstance(extra, dict) else None
    source_dir = brief["run_id"]
    entry = db.ingest_log_entry(brief["run_id"])
    if entry and entry.get("source_dir"):
        source_dir = entry["source_dir"]
    disk_exists = False
    if benchmark_root:
        disk_exists = os.path.isdir(_disk_path(benchmark_root, source_dir))
    deletable = ingest_source == _MANUAL_UPLOAD
    reason = ""
    if not deletable:
        reason = (
            "Scanner 扫入的记录不可删"
            if not ingest_source
            else f"来源为 {ingest_source!r}，仅 manual_upload 可删"
        )
    return {
        "run_id": brief["run_id"],
        "run_timestamp": brief.get("run_timestamp"),
        "model": brief.get("model"),
        "model_version": brief.get("model_version"),
        "framework": brief.get("framework"),
        "framework_version": brief.get("framework_version"),
        "gpu_type": brief.get("gpu_type"),
        "deployment_mode": brief.get("deployment_mode"),
        "gpu_count": brief.get("gpu_count"),
        "num_metrics": brief.get("num_metrics", 0),
        "benchmark_kind": brief.get("benchmark_kind"),
        "ingest_source": ingest_source,
        "source_dir": source_dir,
        "disk_exists": disk_exists,
        "deletable": deletable,
        "undeletable_reason": reason,
    }


def list_briefs(
    db,
    *,
    kind: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    keyword: str | None = None,
    benchmark_root: str | None = None,
) -> list[dict]:
    """记录管理页列表。"""
    bk = schema.resolve_kind(kind).name
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = _DEFAULT_LIMIT
    lim = max(1, min(lim, _MAX_LIMIT))
    briefs = db.list_run_briefs(kind=bk, limit=lim, keyword=keyword)
    return [_annotate_brief(db, b, benchmark_root) for b in briefs]


def preview_delete(
    db,
    run_id: str,
    *,
    kind: str | None = None,
    benchmark_root: str,
    dir_pattern: str,
) -> dict:
    """跑完护栏但不动手，供前端二次确认展示。"""
    bk = resolve_kind_of(db, run_id, kind)
    doc = _load_doc(db, run_id, bk)
    _assert_manual_upload(doc)
    source_dir = _source_dir_of(db, run_id)
    disk_path, disk_exists = _guard_path(
        source_dir, benchmark_root=benchmark_root, dir_pattern=dir_pattern)
    metrics = doc.get("metrics") or []
    return {
        "run_id": run_id,
        "source_dir": source_dir,
        "disk_path": disk_path,
        "disk_exists": disk_exists,
        "benchmark_kind": bk,
        "table": schema.table_for(bk),
        "model": doc.get("model"),
        "framework": doc.get("framework"),
        "gpu_type": doc.get("gpu_type"),
        "num_metrics": len(metrics) if isinstance(metrics, list) else 0,
        "ingest_source": (doc.get("extra") or {}).get("ingest_source"),
    }


def delete_run(
    db,
    run_id: str,
    *,
    kind: str | None = None,
    benchmark_root: str,
    dir_pattern: str,
) -> dict[str, Any]:
    """
    删除一条 manual_upload 记录：先 rmtree 目录，再单事务删表行 + 台账。
    目录已不存在不算失败（崩溃重试路径）。
    """
    bk = resolve_kind_of(db, run_id, kind)
    doc = _load_doc(db, run_id, bk)
    _assert_manual_upload(doc)
    source_dir = _source_dir_of(db, run_id)
    disk_path, disk_exists = _guard_path(
        source_dir, benchmark_root=benchmark_root, dir_pattern=dir_pattern)

    removed_dir = False
    if disk_exists:
        try:
            shutil.rmtree(disk_path)
            removed_dir = True
        except OSError as e:
            raise RunDeleteError(f"删除目录失败: {disk_path}: {e}") from e

    removed_row, removed_log = db.delete_run_and_log(run_id, source_dir, kind=bk)
    return {
        "ok": True,
        "run_id": run_id,
        "source_dir": source_dir,
        "disk_path": disk_path,
        "removed_dir": removed_dir,
        "removed_row": removed_row,
        "removed_log": removed_log,
        "benchmark_kind": bk,
    }
