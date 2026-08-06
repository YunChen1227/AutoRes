"""
手工上传入库（result.csv + 启动命令文本 + 表单元信息）。

场景：数据分散在不同子系统/地区，未落到 Scanner 扫描的 NAS 目录下，
由测试人员在前端页面直接提交一份整理好的 CSV 与启动命令文本。

与目录流的一致性保证：
  - CSV 解析复用 scanner.parser 的行→metric 逻辑（同样的列名归一与数值转换）；
  - 启动参数提取复用 tools/to_csv.py 的规则（见 launch_params 模块）；
  - 产出的文档结构与 parse_run_dir 完全一致，走同一个 db.insert_run。
差异仅在于元信息来源：目录流读 metadata.json，上传流读表单字段。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import importlib.util
import os
import threading

from autores.common.logging import get_logger
from autores.db import schema
from autores.scanner.parser import ParseError, _to_number
from autores.server.ingest import launch_params
from autores.server.ingest.csv_columns import (
    build_header_map,
    check_required_dimensions,
    format_mapping_summary,
)

log = get_logger("upload")

# 表单必填的元信息字段（metadata.json 在上传流里的替代品）
# 模型无版本时只填 model；model_version 入库写空串，不要求用户填写。
REQUIRED_META = ["framework", "framework_version", "model", "gpu_type"]

_GPU_PRESETS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "tools", "gpu_memory_presets.py",
)
_gpu_module = None
_gpu_lock = threading.Lock()


def _load_gpu_presets():
    """按路径加载 tools/gpu_memory_presets.py，显卡下拉与校验共用同一份型号表。"""
    global _gpu_module
    if _gpu_module is not None:
        return _gpu_module
    with _gpu_lock:
        if _gpu_module is not None:
            return _gpu_module
        spec = importlib.util.spec_from_file_location("_autores_gpu_presets", _GPU_PRESETS)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载显卡型号表: {_GPU_PRESETS}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _gpu_module = mod
    return _gpu_module


def supported_gpu_types() -> list[str]:
    """上传表单可选显卡型号（与 gpu_memory_presets.GPU_MEMORY_GIB 一致）。"""
    return sorted(_load_gpu_presets().GPU_MEMORY_GIB.keys())

# run_id 前缀，用于在库里区分手工上传与 Scanner 扫描的记录
UPLOAD_PREFIX = "upload_"

# 上传体积上限（防止超大文件打满内存/磁盘）
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_TXT_BYTES = 64 * 1024


class UploadError(Exception):
    """上传内容非法。上层转成 400 反馈给用户（可修正后重试）。"""


def _decode(raw: bytes, label: str, limit: int) -> str:
    if not raw or not raw.strip():
        raise UploadError(f"{label} 为空")
    if len(raw) > limit:
        raise UploadError(f"{label} 超过大小上限（{limit // 1024} KB）")
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise UploadError(f"{label} 编码无法识别（请用 UTF-8 保存）")


def parse_csv_text(text: str) -> list[dict]:
    """
    把 CSV 文本转为 metric 记录列表。
    表头会先 remap 到 to_csv.py 规范列名；列名归一与数值转换与 scanner 一致。
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise UploadError("CSV 无表头")

    header_map = build_header_map(list(reader.fieldnames))
    missing = check_required_dimensions(header_map)
    if missing:
        raw = [h.strip() for h in reader.fieldnames if h]
        raise UploadError(
            f"CSV 缺少必需列 {missing}；当前表头: {raw}。"
            "请确认含 Input Length/Input_Length 与 Concurrency，或改用 to_csv.py 产出。"
        )

    remapped = format_mapping_summary(header_map)
    if remapped:
        log.info("CSV 列名已自动映射", extra={"fields": {"mapping": remapped}})

    metrics: list[dict] = []
    for lineno, row in enumerate(reader, start=2):
        record: dict = {}
        for col, raw in row.items():
            if col is None:
                continue
            col = col.strip()
            if not col:
                continue
            canon = header_map.get(col)
            if canon is None:
                continue
            key = canon.lower() if canon in schema.METRIC_DIMENSION_KEYS else canon
            record[key] = _to_number(raw.strip() if isinstance(raw, str) else raw)
        for dim in ("input_length", "concurrency"):
            if record.get(dim) is None:
                raise UploadError(f"CSV 第 {lineno} 行的 {dim} 为空或非数值")
        metrics.append(record)

    if not metrics:
        raise UploadError("CSV 无数据行")
    return metrics


def parse_launch_txt(text: str) -> str:
    """
    从启动命令文本提取命令原文。
    允许 # 注释行与空行；其余非空行拼接为一条命令
    （便于直接粘贴带反斜杠换行的多行命令）。
    """
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s.rstrip("\\").strip())  # 去掉续行反斜杠
    cmd = " ".join(lines).strip()
    if not cmd:
        raise UploadError("启动命令无有效内容（除注释与空行外为空）")
    return cmd


def validate_meta(form: dict) -> dict:
    """校验表单元信息，返回清洗后的 dict。"""
    meta = {}
    for key in REQUIRED_META:
        val = (form.get(key) or "").strip()
        if not val:
            raise UploadError(f"缺少必填字段: {key}")
        meta[key] = val

    # 模型版本可选：无版本时存空串，库表仍保留该列以便与落盘流对齐
    meta["model_version"] = (form.get("model_version") or "").strip()

    allowed = launch_params.supported_frameworks()
    if meta["framework"] not in allowed:
        raise UploadError(f"framework 必须是 {allowed} 之一，收到: {meta['framework']}")

    gpus = supported_gpu_types()
    if meta["gpu_type"] not in gpus:
        raise UploadError(f"gpu_type 必须是 {gpus} 之一，收到: {meta['gpu_type']}")
    return meta


def _make_run_id(db, when: datetime) -> str:
    """
    生成 run_id：upload_YYYYMMDD_HHMMSS。
    同秒冲突时追加 _1、_2…（上传是人工低频操作，冲突极少）。
    """
    base = UPLOAD_PREFIX + when.strftime("%Y%m%d_%H%M%S")
    # 注意：fetch_runs 返回的是文档形态（run_id 已映射为 _id），这里要按库内列名查，
    # 故用 count_runs 直接对 run_id 列计数，避免取回整行再取错键。
    if db.count_runs("run_id = ?", [base]) == 0:
        return base
    for i in range(1, 1000):
        cand = f"{base}_{i}"
        if db.count_runs("run_id = ?", [cand]) == 0:
            return cand
    raise UploadError("run_id 冲突过多，请稍后重试")


def resolve_launch_text(launch_text: str | None) -> str:
    """从直接输入的文本解析启动命令。"""
    if not launch_text or not launch_text.strip():
        raise UploadError("请填写启动命令")
    raw = launch_text.strip()
    if len(raw.encode("utf-8")) > MAX_TXT_BYTES:
        raise UploadError(f"启动命令超过大小上限（{MAX_TXT_BYTES // 1024} KB）")
    return parse_launch_txt(raw)


def build_doc(db, meta: dict, csv_text: str, launch_cmd: str) -> dict:
    """
    组装可直接 insert_run 的 test_runs 文档。
    结构与 scanner.parser.parse_run_dir 的产出保持一致。
    """
    metrics = parse_csv_text(csv_text)
    params, extra = launch_params.extract(meta["framework"], launch_cmd)

    # 标注来源，便于日后区分手工上传与自动扫描的记录
    extra = dict(extra)
    extra["ingest_source"] = "manual_upload"

    now = datetime.now(timezone.utc)
    return {
        "_id": _make_run_id(db, now),
        "run_timestamp": now,
        "model": meta["model"],
        "model_version": meta["model_version"],
        "framework": meta["framework"],
        "framework_version": meta["framework_version"],
        "gpu_type": meta["gpu_type"],
        "launch_cmd": launch_cmd,
        "params": params,
        "extra": extra,
        "metrics": metrics,
        "created_at": now,
    }


def ingest(db, meta_form: dict, csv_bytes: bytes, launch_text: str) -> dict:
    """
    完整上传流程：校验 → 解析 → 入库 → 记台账。
    返回入库摘要；任何非法输入抛 UploadError（上层转 400）。
    """
    meta = validate_meta(meta_form)
    csv_text = _decode(csv_bytes, "CSV 文件", MAX_CSV_BYTES)
    launch_cmd = resolve_launch_text(launch_text)

    doc = build_doc(db, meta, csv_text, launch_cmd)
    db.insert_run(doc)
    # 台账用 run_id 自身作为 source_dir（上传无源目录），避免与扫描目录名冲突
    db.mark_ingested(doc["_id"], doc["_id"])

    return {
        "run_id": doc["_id"],
        "num_metrics": len(doc["metrics"]),
        "launch_cmd": doc["launch_cmd"],
        "params": doc["params"],
        "unrecognized": doc["extra"].get("unrecognized", []),
    }
