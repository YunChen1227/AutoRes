"""
上传 CSV 表头 → to_csv.py 规范列名的别名映射。

bench 工具 / 手工整理导出的 CSV 表头写法不一（空格、缩写、是否带 (ms)），
入库前统一 remap 到 METRIC_FIELD_MAP 的固定 schema。
"""
from __future__ import annotations

import re

from autores.db import schema

# 与 tools/to_csv.py METRIC_FIELD_MAP 键序一致
CANONICAL_COLUMNS: tuple[str, ...] = (
    "Input_Length",
    "Concurrency",
    "Request_Throughput",
    "Input_Throughput",
    "Output_Throughput",
    "Total_Throughput",
    "TTFT_Mean(ms)",
    "TTFT_Median(ms)",
    "TTFT_P95(ms)",
    "TTFT_P99(ms)",
    "TPOT_Mean(ms)",
    "TPOT_Median(ms)",
    "TPOT_P95(ms)",
    "TPOT_P99(ms)",
    "ITL_Mean(ms)",
    "ITL_Median(ms)",
    "ITL_P95(ms)",
    "ITL_P99(ms)",
    "E2E_Mean(ms)",
    "E2E_Median(ms)",
    "E2E_P95(ms)",
    "E2E_P99(ms)",
    "Completed",
    "Total_Input_Tokens",
    "Total_Output_Tokens",
    # KV cache 命中率：强制对齐，跨框架可比（统一 0-100 百分比）
    "KV_Cache_Hit_Rate(%)",
    # spec decoding 接受率：不对齐，跨框架不可比（框架前缀列各存各的）
    "SGLang_Spec_Accept_Length",
    "vLLM_Spec_Accept_Rate(%)",
    "vLLM_Spec_Accept_Length",
)

_CANONICAL_SET = frozenset(CANONICAL_COLUMNS)

# spec decoding 规范列，按框架分组。上传时据"哪边列有值"粗判 bench_framework：
#   只有 vllm 列有值 → vllm；只有 sglang 列有值 → sglang；都有/都无 → 需手填。
SPEC_COLUMNS: dict[str, tuple[str, ...]] = {
    "sglang": ("SGLang_Spec_Accept_Length",),
    "vllm": ("vLLM_Spec_Accept_Rate(%)", "vLLM_Spec_Accept_Length"),
}


def _norm_key(name: str) -> str:
    """忽略大小写、空格、下划线、括号、单位后缀，便于别名匹配。"""
    s = name.strip().lower()
    s = re.sub(r"\(ms\)\s*$", "", s)
    s = re.sub(r"\s*\(ms\)\s*", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


# 不参与入库的列（文件名等元数据）
_SKIP_KEYS = frozenset({
    _norm_key("File_Name"),
    _norm_key("Filename"),
    _norm_key("File"),
    _norm_key("Source"),
})

# 规范列名 + 常见别名 → 规范列名
_ALIASES: dict[str, str] = {_norm_key(c): c for c in CANONICAL_COLUMNS}

_MANUAL_ALIASES: dict[str, str] = {
    # 维度
    "inputlength": "Input_Length",
    "inputlen": "Input_Length",
    "randominputlen": "Input_Length",
    "maxconcurrency": "Concurrency",
    "concurrencylevel": "Concurrency",
    # 吞吐（缩写 Thr / Throughput）
    "requestthr": "Request_Throughput",
    "requestthroughput": "Request_Throughput",
    "reqthroughput": "Request_Throughput",
    "reqthr": "Request_Throughput",
    "inputthr": "Input_Throughput",
    "inputthroughput": "Input_Throughput",
    "outputthr": "Output_Throughput",
    "outputthroughput": "Output_Throughput",
    "totaltokenthr": "Total_Throughput",
    "totalthr": "Total_Throughput",
    "totaltokenthroughput": "Total_Throughput",
    "totalthroughput": "Total_Throughput",
    "tokenthroughput": "Total_Throughput",
    # TTFT / TPOT / ITL（带或不带 (ms)、Mean/Median/P99）
    "ttftmean": "TTFT_Mean(ms)",
    "ttftmedian": "TTFT_Median(ms)",
    "ttftp95": "TTFT_P95(ms)",
    "ttftp99": "TTFT_P99(ms)",
    "tpotmean": "TPOT_Mean(ms)",
    "tpotmedian": "TPOT_Median(ms)",
    "tpotp95": "TPOT_P95(ms)",
    "tpotp99": "TPOT_P99(ms)",
    "itlmean": "ITL_Mean(ms)",
    "itlmedian": "ITL_Median(ms)",
    "itlp95": "ITL_P95(ms)",
    "itlp99": "ITL_P99(ms)",
    # E2E / E2EL / 手工导出 typo（mSE2E）
    "e2emean": "E2E_Mean(ms)",
    "e2emedian": "E2E_Median(ms)",
    "e2ep95": "E2E_P95(ms)",
    "e2ep99": "E2E_P99(ms)",
    "e2elmean": "E2E_Mean(ms)",
    "e2elmedian": "E2E_Median(ms)",
    "e2elp95": "E2E_P95(ms)",
    "e2elp99": "E2E_P99(ms)",
    "mse2emean": "E2E_Mean(ms)",
    "mse2emedian": "E2E_Median(ms)",
    "mse2ep95": "E2E_P95(ms)",
    "mse2ep99": "E2E_P99(ms)",
    "meane2elatencyms": "E2E_Mean(ms)",
    "mediane2elatencyms": "E2E_Median(ms)",
    # 计数
    "completedrequests": "Completed",
    "numcompleted": "Completed",
    "totalinputtokens": "Total_Input_Tokens",
    "totaloutputtokens": "Total_Output_Tokens",
    # KV cache 命中率（对齐列）—— 兼容各种拉平写法
    "cachehitrate": "KV_Cache_Hit_Rate(%)",
    "cachehitratepct": "KV_Cache_Hit_Rate(%)",
    "kvhitrate": "KV_Cache_Hit_Rate(%)",
    "prefixcachehitrate": "KV_Cache_Hit_Rate(%)",
    "kvcachehitratepct": "KV_Cache_Hit_Rate(%)",
    # spec decoding（框架专属，非对齐列）
    "sglangacceptlength": "SGLang_Spec_Accept_Length",
    "sglangspecacceptlength": "SGLang_Spec_Accept_Length",
    "vllmspecacceptrate": "vLLM_Spec_Accept_Rate(%)",
    "specdecodeacceptancerate": "vLLM_Spec_Accept_Rate(%)",
    "vllmspecacceptlength": "vLLM_Spec_Accept_Length",
    "specdecodeacceptancelength": "vLLM_Spec_Accept_Length",
}

for k, v in _MANUAL_ALIASES.items():
    _ALIASES.setdefault(k, v)


def resolve_column(raw_header: str) -> str | None:
    """
    将原始表头映射为规范列名。
    返回 None 表示跳过该列（未知或明确忽略）。
    """
    name = raw_header.strip()
    if not name:
        return None
    if name in _CANONICAL_SET:
        return name
    key = _norm_key(name)
    if key in _SKIP_KEYS:
        return None
    if key in _ALIASES:
        return _ALIASES[key]
    return None


def build_header_map(raw_headers: list[str]) -> dict[str, str | None]:
    """原始表头 → 规范列名（或 None 跳过）。"""
    return {h.strip(): resolve_column(h) for h in raw_headers if h and h.strip()}


def mapped_canonical_headers(header_map: dict[str, str | None]) -> set[str]:
    return {v for v in header_map.values() if v}


def format_mapping_summary(header_map: dict[str, str | None]) -> list[str]:
    """供日志/调试：['Input Length → Input_Length', ...]"""
    lines = []
    for raw, canon in header_map.items():
        if canon is None:
            continue
        if raw != canon:
            lines.append(f"{raw} → {canon}")
    return lines


def check_required_dimensions(header_map: dict[str, str | None]) -> list[str]:
    """返回仍缺失的维度列（规范名）。"""
    present = mapped_canonical_headers(header_map)
    return [k for k in schema.METRIC_DIMENSION_KEYS if k not in present]
