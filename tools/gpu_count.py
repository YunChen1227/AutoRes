"""
由启动参数推算实际占用 GPU 卡数（入库时与报告层共用同一份逻辑）。

卡数公式（2026-08，与 autores/server/report/hardware.py 注释一致）：
  vLLM   : gpus = tp × pp × dp（EP/DCP 不额外占卡）
  SGLang : 未开 dp_attention → tp × pp × dp；开启 dp_attention → tp × pp
  PD 分离: 分别在 prefill / decode 命令上按上式各算一套，总卡数 = 两者之和
           （router 不计）

并行度默认值（param_map.py 静态默认 tp=pp=dp=1）在算卡数前回填，
确保"命令里没写 tp"与"写了 tp=1"得到相同卡数——与 design.md D-默认值 一致。
"""
from __future__ import annotations

# 算卡数前需保证有值的并行度字段（param_map 静态默认均为 1）
_PARALLEL_DEFAULTS = ("tp", "pp", "dp")


def _as_int(value, default: int = 1) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if v >= 1 else default


def fill_parallel_defaults(params: dict) -> dict:
    """
    为算卡数回填 tp/pp/dp 静态默认值（缺省 → 1，就地修改并返回 params）。
    仅回填并行度，不动其它 DERIVED 参数。
    """
    for key in _PARALLEL_DEFAULTS:
        if params.get(key) is None:
            params[key] = 1
    return params


def effective_gpu_count(framework: str, params: dict | None) -> int:
    """由并行参数推算实际占用卡数（会先按默认值理解缺省的 tp/pp/dp）。"""
    p = fill_parallel_defaults(dict(params or {}))
    tp = _as_int(p.get("tp"))
    pp = _as_int(p.get("pp"))
    dp = _as_int(p.get("dp"))

    if framework == "sglang" and bool(p.get("dp_attention")):
        return tp * pp
    return tp * pp * dp


def annotate_gpu_count(framework: str, params: dict, extra: dict | None = None) -> int:
    """
    单机/分布式：回填 tp/pp/dp 默认值到 params，计算 gpu_count 写入 extra。
    返回 gpu_count。
    """
    fill_parallel_defaults(params)
    count = effective_gpu_count(framework, params)
    if extra is not None:
        extra["gpu_count"] = count
    return count


def annotate_pd_gpu_counts(framework: str, prefill_params: dict, decode_params: dict) -> tuple[int, int, int]:
    """
    PD 分离：分别回填并计算 prefill / decode 卡数，返回 (prefill, decode, total)。
    """
    pf = annotate_gpu_count(framework, prefill_params)
    dc = annotate_gpu_count(framework, decode_params)
    return pf, dc, pf + dc
