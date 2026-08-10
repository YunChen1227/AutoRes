"""
硬件规模换算：读取入库时已算好的 gpu_count，并为"对齐卡数"的弱扩展对比做吞吐换算。

卡数在入库阶段（tools/to_csv.py extract_launch_params / PD extract_role）已按
tools/gpu_count.py 回填 tp/pp/dp 默认值并写入 extra.gpu_count（或表列 gpu_count）。
本模块**不再**从 params 反推 PD 总卡数，只消费入库结果；老数据无 gpu_count 时
才回退到 effective_gpu_count(params) 兜底。

弱扩展对比说明见下方注释。
"""
from __future__ import annotations

import importlib.util
import os

# 会随卡数线性换算的吞吐类指标（其余指标保持原值）
THROUGHPUT_METRICS: frozenset[str] = frozenset({
    "Request_Throughput",
    "Input_Throughput",
    "Output_Throughput",
    "Total_Throughput",
})

_GPU_COUNT_MOD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "tools", "gpu_count.py",
)
_GPU_PRESETS_MOD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "tools", "gpu_memory_presets.py",
)
_gc = None
_gp = None


def _load_gc():
    global _gc
    if _gc is not None:
        return _gc
    spec = importlib.util.spec_from_file_location("_autores_gpu_count", _GPU_COUNT_MOD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _gc = mod
    return _gc


def _load_gpu_presets():
    global _gp
    if _gp is not None:
        return _gp
    spec = importlib.util.spec_from_file_location("_autores_gpu_presets_hw", _GPU_PRESETS_MOD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _gp = mod
    return _gp


def gpus_per_machine(gpu_type: str) -> int:
    return _load_gpu_presets().gpus_per_machine(gpu_type)


def effective_gpu_count(framework: str, params: dict | None) -> int:
    """兜底：从 params 推算卡数（老数据或未走 extract 的路径）。"""
    return _load_gc().effective_gpu_count(framework, params)


def gpu_count_of_doc(doc: dict) -> int:
    """读取文档上的 gpu_count（入库时已算好）；缺失时从 params 兜底。"""
    if doc.get("gpu_count") is not None:
        return int(doc["gpu_count"])
    extra = doc.get("extra") or {}
    if extra.get("gpu_count") is not None:
        return int(extra["gpu_count"])
    return effective_gpu_count(doc.get("framework", ""), doc.get("params"))


def unit_of(gpus: int, gpu_type: str = "") -> tuple[str, int]:
    """卡数 → (单位, 数量)。整机器倍数按机器计，否则按卡。"""
    per = gpus_per_machine(gpu_type)
    if gpus > 0 and gpus % per == 0:
        return ("machine", gpus // per)
    return ("card", gpus)


def unit_desc(gpus: int, gpu_type: str = "") -> str:
    """人类可读的规模描述，如 '16卡(2机)' 或 '4卡'。"""
    unit, count = unit_of(gpus, gpu_type)
    if unit == "machine":
        return f"{gpus}卡({count}机)"
    return f"{gpus}卡"


def _scaled_number(value, scale: float):
    """按比例换算一个数值；None 保持 None；整数结果去小数。"""
    if value is None or isinstance(value, bool):
        return value
    if not isinstance(value, (int, float)):
        return value
    scaled = value * scale
    if float(scaled).is_integer():
        return int(scaled)
    return scaled


def scale_metrics(metrics: list[dict], scale: float) -> list[dict]:
    """
    对一份 metric 记录按卡数比例做弱扩展换算：
      - 吞吐类 × scale
      - concurrency × scale（对齐每卡并发）
      - 其余（延迟类、input_length 等）保持不变
    """
    out: list[dict] = []
    for m in metrics:
        rec = dict(m)
        if scale != 1:
            if rec.get("concurrency") is not None:
                rec["concurrency"] = _scaled_number(rec["concurrency"], scale)
            for key in THROUGHPUT_METRICS:
                if key in rec:
                    rec[key] = _scaled_number(rec[key], scale)
        out.append(rec)
    return out


def annotate_and_scale(docs: list[dict]) -> bool:
    """
    就地为每个 doc 读取 gpu_count 并做弱扩展换算，使所有 doc 归一到最大卡数。
    在 doc 上写入 _gpus / _scale / _unit / _unit_count 注记；
    返回是否发生了实际换算（存在 scale != 1）。
    """
    if not docs:
        return False

    for doc in docs:
        doc["_gpus"] = gpu_count_of_doc(doc)

    max_gpus = max(doc["_gpus"] for doc in docs)
    scaled_any = False
    for doc in docs:
        gpus = doc["_gpus"] or 1
        scale = max_gpus / gpus if gpus else 1
        doc["_scale"] = scale
        unit, count = unit_of(gpus, doc.get("gpu_type", ""))
        doc["_unit"] = unit
        doc["_unit_count"] = count
        if scale != 1:
            scaled_any = True
            doc["metrics"] = scale_metrics(doc.get("metrics", []), scale)
    return scaled_any
