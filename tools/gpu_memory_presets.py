"""
显卡显存容量 → SGLang 显存相关启动参数推导值（D17 补充）。

────────────────────────────────────────────────────────────────────────
背景
────────────────────────────────────────────────────────────────────────
tools/param_map.py 把 mem_fraction / chunked_prefill_size / page_size 等参数
标记为 DERIVED（见该文件顶部说明），因为它们没有固定标量默认值，是
SGLang 在 server_args.py:_handle_gpu_memory_settings 里按 GPU 显存容量
（外加 tp_size）在运行时算出来的。

本模块把该函数的分档规则搬到这里（同一份源码逻辑，非重新发明），
外加一份用户实际使用的显卡型号 → 显存容量对照表，使得"给定显卡型号"
就能算出参考值，不必真的起一个 SGLang 进程。

────────────────────────────────────────────────────────────────────────
重要限制（务必读完再用）
────────────────────────────────────────────────────────────────────────
1. `_handle_gpu_memory_settings` 的分档阈值是 SGLang 针对 **NVIDIA GPU**
   总结的经验值（代码注释里点名 T4/A10/4090/A100/H100/H20/H200/B200/MI300）。
   910B3/910B4（华为昇腾）、C550（沐曦）、PPU810E/PPU890（阿里平头哥）都不是
   NVIDIA 架构，SGLang 官方从未在这些芯片上验证过该分档表。
   这里给出的是"显存容量凑巧落在哪个区间"的数值参考，不代表 SGLang 已验证
   适配这些芯片——多数实际会走各自厂商的推理框架（如昇腾走 MindIE）。
   （2026-08-04 用户已确认知悉此限制。）

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

# ── 用户常用显卡：型号 → 显存容量 (GiB) ──────────────────────────────────
# 来源：网络检索 + 用户确认，见 scratchpad/gpu_specs.md。
# 仅显存容量参与 SGLang 的分档计算，带宽/算力/类型等字段不影响本模块结果，
# 因此这里不重复存那些字段（避免造成"这些字段也用于计算"的误导）。
GPU_MEMORY_GIB = {
    "H20-141G": 141,
    "H20-96G": 96,
    "910B3": 64,
    "910B4-32G": 32,   # 910B4 显存容量可选 32/64GB，按你实际采购型号二选一
    "910B4-64G": 64,
    "C550": 64,
    "PPU810E": 96,
    "PPU890": 144,
}


def gpus_per_machine(gpu_type: str) -> int:
    """
    每台机器的 GPU 卡数（用于按机器/按卡规模描述与对齐标注）。

    默认 8 卡 = 1 机（H20/H800 等）；PPU 系列 16 卡 = 1 机。
    """
    if (gpu_type or "").startswith("PPU"):
        return 16
    return 8


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
        gpu_mem_gib            显存容量 (GiB)，来自 GPU_MEMORY_GIB
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

    抛出 KeyError 如果 gpu_name 不在 GPU_MEMORY_GIB 里。
    """
    if gpu_name not in GPU_MEMORY_GIB:
        raise KeyError(
            f"未知显卡型号: {gpu_name!r}，可用型号: {sorted(GPU_MEMORY_GIB)}"
        )
    gpu_mem_gib = GPU_MEMORY_GIB[gpu_name]
    gpu_mem_mib = gpu_mem_gib * 1024
    tier = _gpu_mem_tier(gpu_mem_mib, tp_size)
    activation_tokens = max(tier["chunked_prefill_size"], 2048)

    is_nvidia = gpu_name.startswith("H20")
    note = (
        "SGLang 分档表按 NVIDIA 系列总结，H20 属已知档位（H20/H200 同档）。"
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
    """对 GPU_MEMORY_GIB 里的全部型号批量计算，便于生成对照表。"""
    return {name: estimate_sglang_memory_params(name, tp_size) for name in GPU_MEMORY_GIB}
