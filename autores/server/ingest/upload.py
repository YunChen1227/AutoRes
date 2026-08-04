"""
手工上传入库（result.csv + 启动命令 txt + 表单元信息）。

场景：数据分散在不同子系统/地区，未落到 Scanner 扫描的 NAS 目录下，
由测试人员在前端页面直接提交一份整理好的 CSV 与一个写有启动命令的 txt。

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

from autores.db import schema
from autores.scanner.parser import ParseError, _to_number
from autores.server.ingest import launch_params

# 表单必填的元信息字段（metadata.json 在上传流里的替代品）
REQUIRED_META = ["framework", "framework_version", "model", "model_version", "gpu_type"]

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
    列名归一与数值转换与 scanner.parser._parse_csv 一致（复用 _to_number）。
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise UploadError("CSV 无表头")

    headers = [h.strip() for h in reader.fieldnames if h]
    # 两个维度列必须在（否则无法定位这行指标属于哪个测试条件）
    missing = [k for k in schema.METRIC_DIMENSION_KEYS if k not in headers]
    if missing:
        raise UploadError(
            f"CSV 缺少必需列 {missing}；当前表头: {headers}。"
            "请确认使用 to_csv.py 产出的固定 schema。"
        )

    metrics: list[dict] = []
    for lineno, row in enumerate(reader, start=2):
        record: dict = {}
        for col, raw in row.items():
            if col is None:
                continue
            col = col.strip()
            if not col:
                continue
            # Input_Length -> input_length，Concurrency -> concurrency（维度用小写）
            key = col.lower() if col in schema.METRIC_DIMENSION_KEYS else col
            record[key] = _to_number(raw.strip() if isinstance(raw, str) else raw)
        # 维度列缺值的行无法参与对齐，直接判错而不是静默丢弃
        for dim in ("input_length", "concurrency"):
            if record.get(dim) is None:
                raise UploadError(f"CSV 第 {lineno} 行的 {dim} 为空或非数值")
        metrics.append(record)

    if not metrics:
        raise UploadError("CSV 无数据行")
    return metrics


def parse_launch_txt(text: str) -> str:
    """
    从 txt 提取启动命令原文。
    允许文件带注释行（# 开头）与空行；其余非空行拼接为一条命令
    （便于用户直接粘贴带反斜杠换行的多行命令）。
    """
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s.rstrip("\\").strip())  # 去掉续行反斜杠
    cmd = " ".join(lines).strip()
    if not cmd:
        raise UploadError("启动命令 txt 无有效内容（除注释与空行外为空）")
    return cmd


def validate_meta(form: dict) -> dict:
    """校验表单元信息，返回清洗后的 dict。"""
    meta = {}
    for key in REQUIRED_META:
        val = (form.get(key) or "").strip()
        if not val:
            raise UploadError(f"缺少必填字段: {key}")
        meta[key] = val

    allowed = launch_params.supported_frameworks()
    if meta["framework"] not in allowed:
        raise UploadError(f"framework 必须是 {allowed} 之一，收到: {meta['framework']}")
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


def build_doc(db, meta: dict, csv_text: str, txt_text: str) -> dict:
    """
    组装可直接 insert_run 的 test_runs 文档。
    结构与 scanner.parser.parse_run_dir 的产出保持一致。
    """
    metrics = parse_csv_text(csv_text)
    launch_cmd = parse_launch_txt(txt_text)
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


def ingest(db, meta_form: dict, csv_bytes: bytes, txt_bytes: bytes) -> dict:
    """
    完整上传流程：校验 → 解析 → 入库 → 记台账。
    返回入库摘要；任何非法输入抛 UploadError（上层转 400）。
    """
    meta = validate_meta(meta_form)
    csv_text = _decode(csv_bytes, "CSV 文件", MAX_CSV_BYTES)
    txt_text = _decode(txt_bytes, "启动命令 txt", MAX_TXT_BYTES)

    doc = build_doc(db, meta, csv_text, txt_text)
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
