"""
硬件规模换算：由启动参数推算实际卡数 / 机器数，并为"对齐卡数"的弱扩展
(weak-scaling) 对比做吞吐换算与并发对齐。

卡数公式（依据上游源码与官方文档核对，2026-08）：
  vLLM   : gpus = tp × pp × dp
           - EP 不额外占卡：MoE 专家分布在 TP×DP 既有 rank 上（--enable-expert-parallel）。
           - DCP(decode_context_parallel) 复用 TP 组，不额外占卡。
  SGLang : 未开 dp_attention : gpus = tp × pp × dp   （每个 DP 副本各自一套 TP 组）
           开启 dp_attention  : gpus = tp × pp        （DP 在 TP 组内，attn_tp = tp // dp）
           - ep / attn_cp 同样在 TP 组内，不额外占卡。

参考：
  - vLLM  docs/serving/parallelism_scaling.md、data_parallel_deployment.md
          （world size = TP×PP×DP；EP 由 TP×DP 组承载）
  - SGLang python/sglang/srt/layers/dp_attention.py:compute_dp_attention_world_info
          （attn_tp_size = tp_size // attn_dp_size // attn_cp_size）
          + 官方示例 `--tp 8 --dp-size 8 --enable-dp-attention` 单机 8 卡即可跑。

机器/卡维度（design 需求 2.2）：
  卡数为 8 的倍数 → 以"机器"为单位（机器数 = 卡数 // 8）；否则以"卡"为单位。

弱扩展对比是否靠谱（需求 2.6，已联网核对）：
  - 吞吐类按卡数比例线性换算 = 归一到"每卡吞吐 × 大卡数"，比较结果即扩展效率
    （偏离理想线性的程度），是评估并行扩展性的标准做法。
  - 并发按同一比例对齐 = 保持"每卡并发"恒定，正是弱扩展该对齐的量。
  - 延迟类(TTFT/TPOT/ITL/E2E)不随卡数线性变化，保持原值，不换算。
  - 前提：较大卡数一侧需存在换算后的并发点，否则无法对齐（填 N/A）。
"""
from __future__ import annotations

# 会随卡数线性换算的吞吐类指标（其余指标保持原值）
THROUGHPUT_METRICS: frozenset[str] = frozenset({
    "Request_Throughput",
    "Input_Throughput",
    "Output_Throughput",
    "Total_Throughput",
})

_GPUS_PER_MACHINE = 8


def _as_int(value, default: int = 1) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if v >= 1 else default


def effective_gpu_count(framework: str, params: dict | None) -> int:
    """由并行参数推算实际占用卡数。缺省的并行度按 1 计。"""
    p = params or {}
    tp = _as_int(p.get("tp"))
    pp = _as_int(p.get("pp"))
    dp = _as_int(p.get("dp"))

    if framework == "sglang" and bool(p.get("dp_attention")):
        # DP attention：dp 在 tp 组内，不额外占卡
        return tp * pp
    return tp * pp * dp


def unit_of(gpus: int) -> tuple[str, int]:
    """卡数 → (单位, 数量)。8 的倍数按机器，否则按卡。"""
    if gpus > 0 and gpus % _GPUS_PER_MACHINE == 0:
        return ("machine", gpus // _GPUS_PER_MACHINE)
    return ("card", gpus)


def unit_desc(gpus: int) -> str:
    """人类可读的规模描述，如 '16卡(2机)' 或 '4卡'。"""
    unit, count = unit_of(gpus)
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
    scale == 1 时原样返回（浅拷贝）。
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
    就地为每个 doc 计算卡数并做弱扩展换算，使所有 doc 归一到最大卡数。
    在 doc 上写入 _gpus / _scale / _unit / _unit_count 注记；
    返回是否发生了实际换算（存在 scale != 1）。
    """
    if not docs:
        return False

    for doc in docs:
        gpus = effective_gpu_count(doc.get("framework"), doc.get("params"))
        doc["_gpus"] = gpus

    max_gpus = max(doc["_gpus"] for doc in docs)
    scaled_any = False
    for doc in docs:
        gpus = doc["_gpus"] or 1
        scale = max_gpus / gpus if gpus else 1
        doc["_scale"] = scale
        unit, count = unit_of(gpus)
        doc["_unit"] = unit
        doc["_unit_count"] = count
        if scale != 1:
            scaled_any = True
            doc["metrics"] = scale_metrics(doc.get("metrics", []), scale)
    return scaled_any
