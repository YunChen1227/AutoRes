"""
显卡显存容量 → SGLang 显存相关启动参数推导值（D17 补充 / D25 注册表）。

────────────────────────────────────────────────────────────────────────
背景
────────────────────────────────────────────────────────────────────────
tools/param_map.py 把 mem_fraction / chunked_prefill_size / page_size 等参数
标记为 DERIVED（见该文件顶部说明），因为它们没有固定标量默认值，是
SGLang 在 server_args.py:_handle_gpu_memory_settings 里按 GPU 显存容量
（外加 tp_size）在运行时算出来的。

本模块把该函数的分档规则搬到这里（同一份源码逻辑，非重新发明），
外加一份用户实际使用的显卡型号注册表（tools/gpu_types.json），使得
"给定显卡型号"就能算出参考值，不必真的起一个 SGLang 进程。

型号表真相源是 JSON 文件（D25），本模块提供读/写与按 mtime 缓存；
页面 / MCP / 上传校验 / 压测机 to_csv 都经这里访问。压测机只需拷走
tools/ 目录（含 gpu_types.json）即可离线使用，不依赖 autores 包。

────────────────────────────────────────────────────────────────────────
重要限制（务必读完再用）
────────────────────────────────────────────────────────────────────────
1. `_handle_gpu_memory_settings` 的分档阈值是 SGLang 针对 **NVIDIA GPU**
   总结的经验值（代码注释里点名 T4/A10/4090/A100/H100/H20/H200/B200/MI300）。
   非 NVIDIA 厂商（huawei / metax / cambricon / t-head 等）SGLang 官方从未
   在这些芯片上验证过该分档表。这里给出的是"显存容量凑巧落在哪个区间"
   的数值参考，不代表 SGLang 已验证适配——多数实际会走各自厂商的推理
   框架（如昇腾走 MindIE）。（2026-08-04 用户已确认知悉此限制。）

2. 【重要修正】mem_fraction_static 的真实计算公式**不是**本模块最初版本
   用的那个简化公式。核对 server_args.py:4750-4785 源码后发现：
   实际赋值在 `if self.mem_fraction_static is None:` 分支，公式是
   `round((gpu_mem - reserved_mem) / gpu_mem, 3)`，其中 gpu_mem 单位是 MiB
   （不是 GB），reserved_mem 由多项累加：
     - 512 (MB 常量底座)
     - activation_tokens * 1.5（activation_tokens 取决于运行模式：
       disaggregation=decode 时用 max_running_requests，否则用
       chunked_prefill_size 或 max_prefill_tokens）
     - tp_size * pp_size / 8 * 1024
     - reserve_for_graph_mb()：依赖 decode/prefill cuda graph 是否启用、
       是否 MLA attention backend、是否 DP attention、dp_size、
       是否 breakable prefill graph + deepep 等一长串运行时状态
     - gpu_mem > 60*1024 时 reserved_mem 有 10GB 地板
     - reserve_for_deepep_a2a_mb()：依赖 moe_a2a_backend 是否为 deepep
   这些依赖项（attention backend、是否 MLA、DP attention 开关、
   moe_a2a_backend...）**都不是显卡参数**，必须知道具体模型架构和
   完整启动参数组合才能算，本模块拿不到、也不该编造。
   因此本模块**只算到 chunked_prefill_size / cuda_graph_max_bs /
   activation_tokens 这几个仅依赖显存容量+tp_size 的中间量**，
   不再给出 mem_fraction_static 的最终估算值（旧版本给的 0.0 是明确的
   计算错误——把 GB/MB 单位搞混了，已删除，不要相信任何早期输出）。

3. 只吃显存容量（GiB）+ tp_size 两个输入，显存带宽/算力/类型均不参与
   chunked_prefill_size / cuda_graph_max_bs 的计算——这是 SGLang 源码
   逻辑本身如此，不是本模块偷懒。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

# ── 注册表路径 ──────────────────────────────────────────────────────────
_DEFAULT_REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "gpu_types.json")

_REGISTRY_LOCK = threading.RLock()
_CACHE: dict[str, Any] | None = None
_CACHE_MTIME: float | None = None
_CACHE_PATH: str | None = None

# 厂商枚举（页面下拉 / MCP / 校验共用）
VENDOR_CHOICES: tuple[str, ...] = (
    "nvidia", "huawei", "metax", "cambricon", "t-head", "other",
)

_REQUIRED_FIELDS = ("name", "memory_gib", "cards_per_machine", "vendor",
                    "released", "note")


def registry_path() -> str:
    """注册表文件路径；可用环境变量 AUTORES_GPU_TYPES_PATH 覆盖。"""
    override = os.environ.get("AUTORES_GPU_TYPES_PATH", "").strip()
    return override or _DEFAULT_REGISTRY


def _normalize_entry(raw: dict) -> dict:
    """补齐缺省字段，返回规范化条目（不改入参）。"""
    return {
        "name": str(raw["name"]),
        "memory_gib": float(raw["memory_gib"]),
        "cards_per_machine": int(raw.get("cards_per_machine", 8)),
        "vendor": str(raw.get("vendor") or "other"),
        "released": bool(raw.get("released", True)),
        "note": str(raw.get("note") or ""),
    }


def _read_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "gpu_types" not in data:
        raise ValueError(f"显卡注册表格式非法（缺 gpu_types）: {path}")
    entries = data["gpu_types"]
    if not isinstance(entries, list):
        raise ValueError(f"显卡注册表 gpu_types 必须是数组: {path}")
    normalized = [_normalize_entry(e) for e in entries]
    # 按 name 排序，保证写回稳定
    normalized.sort(key=lambda e: e["name"].lower())
    return {"version": int(data.get("version", 1)), "gpu_types": normalized}


def _load_registry_unlocked(force: bool = False) -> dict:
    """调用方必须已持有 _REGISTRY_LOCK。"""
    global _CACHE, _CACHE_MTIME, _CACHE_PATH
    path = registry_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError as e:
        raise FileNotFoundError(
            f"显卡注册表不存在: {path}（可用 AUTORES_GPU_TYPES_PATH 指定）"
        ) from e
    if (not force
            and _CACHE is not None
            and _CACHE_PATH == path
            and _CACHE_MTIME == mtime):
        return _CACHE
    data = _read_file(path)
    _CACHE = data
    _CACHE_MTIME = mtime
    _CACHE_PATH = path
    return data


def load_registry(force: bool = False) -> dict:
    """
    读注册表；按 mtime 缓存，文件被外部改写后下次调用自动重读
    （页面 / MCP 改完不用重启服务）。
    """
    with _REGISTRY_LOCK:
        return _load_registry_unlocked(force)


def _write_registry(data: dict) -> dict:
    """原子写回：先写临时文件再 os.replace；更新缓存。调用方须已持锁。"""
    global _CACHE, _CACHE_MTIME, _CACHE_PATH
    path = registry_path()
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)

    payload = {
        "version": int(data.get("version", 1)),
        "gpu_types": [_normalize_entry(e) for e in data["gpu_types"]],
    }
    payload["gpu_types"].sort(key=lambda e: e["name"].lower())

    fd, tmp = tempfile.mkstemp(prefix=".gpu_types_", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    mtime = os.path.getmtime(path)
    _CACHE = payload
    _CACHE_MTIME = mtime
    _CACHE_PATH = path
    return payload


def all_types() -> list[dict]:
    """返回全部型号条目（副本，按 name 排序）。"""
    return [dict(e) for e in load_registry()["gpu_types"]]


def get_type(name: str) -> dict | None:
    """按型号名取条目；不存在返回 None。"""
    if not name:
        return None
    for e in load_registry()["gpu_types"]:
        if e["name"] == name:
            return dict(e)
    return None


def memory_map() -> dict[str, float]:
    """型号 → 显存 GiB（兼容旧 GPU_MEMORY_GIB 用法）。"""
    return {e["name"]: float(e["memory_gib"]) for e in load_registry()["gpu_types"]}


def upsert_type(entry: dict) -> dict:
    """
    按 name 新增或覆盖一条型号；返回规范化后的条目。
    不做业务校验（那是 autores/server/gpu_types.py 的职责）。
    """
    normalized = _normalize_entry(entry)
    with _REGISTRY_LOCK:
        data = dict(_load_registry_unlocked(force=True))
        types = list(data["gpu_types"])
        replaced = False
        for i, e in enumerate(types):
            if e["name"] == normalized["name"]:
                types[i] = normalized
                replaced = True
                break
        if not replaced:
            types.append(normalized)
        data["gpu_types"] = types
        _write_registry(data)
    return dict(normalized)


def delete_type(name: str) -> bool:
    """按 name 删除；存在并删除返回 True，不存在返回 False。"""
    if not name:
        return False
    with _REGISTRY_LOCK:
        data = dict(_load_registry_unlocked(force=True))
        before = len(data["gpu_types"])
        types = [e for e in data["gpu_types"] if e["name"] != name]
        if len(types) == before:
            return False
        data["gpu_types"] = types
        _write_registry(data)
    return True


def __getattr__(name: str):
    """
    PEP 562：外部 `gmp.GPU_MEMORY_GIB` 继续可用，且每次取最新 memory_map。
    模块内部请走 load_registry() / memory_map()，裸名查找不触发本函数。
    """
    if name == "GPU_MEMORY_GIB":
        return memory_map()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def gpus_per_machine(gpu_type: str) -> int:
    """
    每台机器的 GPU 卡数（用于按机器/按卡规模描述与对齐标注）。
    读注册表 cards_per_machine；未知型号回退 8。
    """
    entry = get_type(gpu_type or "")
    if entry is None:
        return 8
    return int(entry["cards_per_machine"])


def _gpu_mem_tier(gpu_mem_mib: float, tp_size: int) -> dict:
    """
    照搬 sglang/srt/server_args.py:_handle_gpu_memory_settings 的分档规则。
    输入显存单位 MiB（对齐源码 `gpu_mem < 20 * 1024` 的写法），tp_size 影响
    A10/4090/5090 档与 A100/H100/H20/H200 档的 max_bs 取值（tp<4 用低值）。
    """
    if gpu_mem_mib < 20 * 1024:
        chunked_prefill_size = 2048
        max_bs = 8
        tier = "T4, 4080"
    elif gpu_mem_mib < 35 * 1024:
        chunked_prefill_size = 2048
        max_bs = 24 if tp_size < 4 else 80
        tier = "A10, 4090, 5090"
    elif gpu_mem_mib < 60 * 1024:
        chunked_prefill_size = 4096
        max_bs = 32 if tp_size < 4 else 160
        tier = "A100(40GB), L40"
    elif gpu_mem_mib < 90 * 1024:
        chunked_prefill_size = 8192
        max_bs = 256 if tp_size < 4 else 512
        tier = "H100, A100(80GB)"
    elif gpu_mem_mib < 160 * 1024:
        chunked_prefill_size = 8192
        max_bs = 256 if tp_size < 4 else 512
        tier = "H20, H200"
    else:
        chunked_prefill_size = 16384
        max_bs = 512
        tier = "B200, MI300"
    return {
        "tier": tier,
        "chunked_prefill_size": chunked_prefill_size,
        "cuda_graph_max_bs": max_bs,
    }


def estimate_sglang_memory_params(gpu_name: str, tp_size: int = 1) -> dict:
    """
    显卡型号 → SGLang 显存相关参数里"只依赖显存容量+tp_size"的那部分。

    返回：
        gpu_mem_gib            显存容量 (GiB)，来自注册表
        matched_tier           命中的 SGLang 分档说明（源码注释原文）
        chunked_prefill_size   该档位下 chunked_prefill_size 的默认值
        cuda_graph_max_bs      该档位下 decode cuda graph 的 max_bs 默认值
        activation_tokens_estimate
                                mem_fraction_static 公式里 activation_tokens
                                的估算（非 disaggregation=decode 场景下
                                = max(chunked_prefill_size, 2048)）
        note                    该显卡是否为 SGLang 官方验证过的 NVIDIA 型号

    不返回 mem_fraction_static：其真实公式还依赖 attention backend 是否
    MLA、DP attention 开关、moe_a2a_backend、disaggregation_mode 等一批
    非硬件参数（见模块顶部说明 2），编一个数字出来比不给还误导人，故不算。
    要拿到准确值，只能实际起一个 SGLang 进程用目标模型+目标启动参数跑一次。

    抛出 KeyError 如果 gpu_name 不在注册表里。
    """
    entry = get_type(gpu_name)
    if entry is None:
        raise KeyError(
            f"未知显卡型号: {gpu_name!r}，可用型号: "
            f"{sorted(e['name'] for e in all_types())}"
        )
    gpu_mem_gib = float(entry["memory_gib"])
    gpu_mem_mib = gpu_mem_gib * 1024
    tier = _gpu_mem_tier(gpu_mem_mib, tp_size)
    activation_tokens = max(tier["chunked_prefill_size"], 2048)

    is_nvidia = entry.get("vendor") == "nvidia"
    note = (
        "SGLang 分档表按 NVIDIA 系列总结，本型号 vendor=nvidia。"
        if is_nvidia else
        "⚠ 非 NVIDIA 架构，SGLang 未针对此芯片验证过该分档表，"
        "以下数值仅为'显存容量落在哪个区间'的参考，不代表官方适配结论。"
    )

    return {
        "gpu_name": gpu_name,
        "gpu_mem_gib": gpu_mem_gib,
        "tp_size": tp_size,
        "matched_tier": tier["tier"],
        "chunked_prefill_size": tier["chunked_prefill_size"],
        "cuda_graph_max_bs": tier["cuda_graph_max_bs"],
        "activation_tokens_estimate": activation_tokens,
        "note": note,
    }


def estimate_all(tp_size: int = 1) -> dict:
    """对注册表里的全部型号批量计算，便于生成对照表。"""
    return {e["name"]: estimate_sglang_memory_params(e["name"], tp_size)
            for e in all_types()}
