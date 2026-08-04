"""
vLLM ↔ SGLang 启动参数配对表（硬编码，D17）。

本表由两边上游源码静态抽取核对而成，不依赖任何模型推断：
  - vLLM   : vllm/engine/arg_utils.py（CLI flag → config 字段）
             + vllm/config/*.py（默认值，含 DEFAULT_* 常量）
  - SGLang : python/sglang/srt/server_args.py（ServerArgs 注解 dataclass，
             parser 由该 dataclass 自动生成，等价于 `sglang serve --help`）

抽取基线 commit：
  vLLM   385d4c084e
  SGLang c113ead98a

────────────────────────────────────────────────────────────────────────
为什么不是"一个名字配一个名字 + 一个默认值"
────────────────────────────────────────────────────────────────────────
核对源码后发现，两边的差异主要不是命名，而是语义：

1. 有些参数**没有标量默认值**，是运行时按硬件/其它参数推导出来的。
   例：SGLang 的 mem_fraction_static 由 GPU 显存档位 × tp_size ×
   chunked_prefill_size × cuda_graph max_bs 推导
   （server_args.py:_handle_gpu_memory_settings）。
   若硬写一个数，在非该硬件上必然是错的——这正是旧表"和实际值差异太大"的根因。
   因此这类参数用 DERIVED 标记，只记录推导依赖与规则，不记录假数字。

2. 有些参数**量纲不同**，即使数值相同也不等价。
   例：vLLM gpu_memory_utilization = 执行器占总显存比例；
       SGLang mem_fraction_static = (权重 + KV pool) / 总显存，
       明确**不含**激活值与 cuda graph buffer。两者都写 0.9 也不是一回事。

3. 有些参数**类型不同**（宽度 vs 布尔开关），不能按数值直接比。
   例：SGLang ep_size:int=1（宽度）vs vLLM enable_expert_parallel:bool=False（开关）。
   两边都不加 EP 时语义相同（都是"不开"），但裸值是 1 vs False。
   → 用 kind="switch_vs_width" + normalize 归一后再比。

所以每个条目除了名字，还带 kind / default / note，供对齐层判断"能不能比、怎么比"。
"""
from __future__ import annotations

# ── 默认值种类 ──────────────────────────────────────────────────────────
STATIC = "static"    # 源码里就是这个字面量，可直接作为标量比较
DERIVED = "derived"  # 运行时推导，无固定标量；只有命令里显式写了才可比
NA = "na"            # 该框架不存在此概念

# ── 语义类型 ────────────────────────────────────────────────────────────
K_INT = "int"
K_FLOAT = "float"
K_STR = "str"
K_BOOL = "bool"
K_SWITCH_WIDTH = "switch_vs_width"  # 一边布尔开关、一边并行宽度
K_INVERTED = "inverted_bool"        # 一边 opt-in、一边 opt-out（极性相反）


def _p(
    key,
    *,
    kind,
    sgl_flags=None,
    vllm_flags=None,
    sgl_default=None,
    vllm_default=None,
    sgl_kind=STATIC,
    vllm_kind=STATIC,
    sgl_derived_from=(),
    vllm_derived_from=(),
    note=None,
    comparable=True,
):
    return {
        "key": key,
        "kind": kind,
        "comparable": comparable,
        "note": note,
        "sglang": {
            "flags": list(sgl_flags or []),
            "default": sgl_default,
            "default_kind": sgl_kind,
            "derived_from": list(sgl_derived_from),
        },
        "vllm": {
            "flags": list(vllm_flags or []),
            "default": vllm_default,
            "default_kind": vllm_kind,
            "derived_from": list(vllm_derived_from),
        },
    }


# ════════════════════════════════════════════════════════════════════════
# 配对表
# ════════════════════════════════════════════════════════════════════════
PARAM_PAIRS = [
    # ── 并行度 ─────────────────────────────────────────────────────────
    # SGLang 显式声明了 vLLM 拼法作为 alias（server_args.py aliases=[...]），
    # 这几项是全表置信度最高的配对。
    _p("tp", kind=K_INT,
       sgl_flags=["--tp-size", "--tp", "--tensor-parallel-size"],
       vllm_flags=["--tensor-parallel-size", "-tp"],
       sgl_default=1, vllm_default=1,
       note="SGLang 的 --tp 并非显式 alias，而是 argparse 对 --tp-size 的前缀缩写"
            "（官方 cookbook 中出现 400 次，必须支持解析）。"
            "--tensor-parallel-size 才是 server_args.py 里显式声明的 alias。"),

    _p("pp", kind=K_INT,
       sgl_flags=["--pp-size", "--pipeline-parallel-size"],
       vllm_flags=["--pipeline-parallel-size", "-pp"],
       sgl_default=1, vllm_default=1),

    _p("dp", kind=K_INT,
       sgl_flags=["--dp-size", "--dp", "--data-parallel-size"],
       vllm_flags=["--data-parallel-size", "-dp"],
       sgl_default=1, vllm_default=1,
       note="同 tp：--dp 是 --dp-size 的 argparse 前缀缩写（cookbook 中 59 次），非显式 alias。"),

    _p("dcp", kind=K_INT,
       sgl_flags=["--dcp-size", "--decode-context-parallel-size"],
       vllm_flags=["--decode-context-parallel-size", "-dcp"],
       sgl_default=1, vllm_default=1,
       note="decode 侧 context parallel。SGLang 另有 attn_cp_size（attention 侧），"
            "vLLM 另有 prefill_context_parallel_size，两者互不对应，各自单列。"),

    _p("ep", kind=K_SWITCH_WIDTH,
       sgl_flags=["--ep-size", "--ep", "--expert-parallel-size"],
       vllm_flags=["--enable-expert-parallel", "-ep"],
       sgl_default=1, vllm_default=False,
       note="⚠ 类型不同：SGLang ep_size 是并行宽度(int, 默认 1 = 不开)；"
            "vLLM enable_expert_parallel 是布尔开关(默认 False)，EP 宽度由 TP/DP 隐式决定。"
            "裸值 1 vs False 看起来不同，实际都表示'未开启 EP'。"
            "必须用 normalize_ep() 归一后再比，否则每份报告都会出现假差异。"),

    # ── 显存 / KV ──────────────────────────────────────────────────────
    _p("mem_fraction", kind=K_FLOAT,
       sgl_flags=["--mem-fraction-static"],
       vllm_flags=["--gpu-memory-utilization"],
       sgl_default=None, sgl_kind=DERIVED,
       sgl_derived_from=["gpu_memory_capacity", "tp_size",
                         "chunked_prefill_size", "cuda_graph_config.decode.max_bs"],
       vllm_default=0.92, vllm_kind=STATIC,
       comparable=False,
       note="⚠ 量纲不同，不可直接比较。"
            "vLLM gpu_memory_utilization = 执行器可用显存 / 总显存（默认 0.92，"
            "注意不是坊间常说的 0.90，vllm/config/cache.py:68）。"
            "SGLang mem_fraction_static = (模型权重 + KV pool) / 总显存，"
            "明确不含激活值与 cuda graph buffer，默认 None 由 "
            "_handle_gpu_memory_settings 按公式推导："
            "(gpu_mem - (chunked_prefill_size*1.5 + max_bs*2)) / gpu_mem。"
            "仅当两边命令都显式写了该 flag 时，才可作为'用户意图'并列展示。"),

    _p("kv_cache_dtype", kind=K_STR,
       sgl_flags=["--kv-cache-dtype"],
       vllm_flags=["--kv-cache-dtype"],
       sgl_default="auto", vllm_default="auto",
       note="vLLM 侧 CLI flag 名为 --kv-cache-dtype，但内部字段叫 CacheConfig.cache_dtype"
            "（arg_utils.py:1198）——按字段名对齐会错配到 SpeculativeConfig.kv_cache_dtype。"),

    _p("page_size", kind=K_INT,
       sgl_flags=["--page-size"],
       vllm_flags=["--block-size"],
       sgl_default=None, sgl_kind=DERIVED,
       sgl_derived_from=["attention_backend", "model_arch"],
       vllm_default=16, vllm_kind=DERIVED,
       vllm_derived_from=["platform"],
       note="同义（KV 分页粒度，token 数）但默认值都不是字面量："
            "vLLM 声明 Field(default=None)，__post_init__ 回填 DEFAULT_BLOCK_SIZE=16，"
            "且部分平台会覆盖（platforms/interface.py）；"
            "SGLang 默认 None，经 arg_groups/overrides.py:_page_size_default 解析。"
            "此处 vllm_default 记的是回填后的生效值 16，非源码字面量 None。"),

    _p("prefix_caching", kind=K_INVERTED,
       sgl_flags=["--disable-radix-cache"],
       vllm_flags=["--enable-prefix-caching", "--no-enable-prefix-caching"],
       sgl_default=True, sgl_kind=STATIC,
       vllm_default=None, vllm_kind=DERIVED,
       vllm_derived_from=["model_arch", "platform"],
       note="⚠ 极性相反且 vLLM 侧默认非字面量："
            "SGLang 默认开启，用 --disable-radix-cache 关闭（opt-out）；"
            "vLLM enable_prefix_caching 声明 None，运行时按模型/平台回填 "
            "default_prefix_caching（arg_utils.py:2624），多数场景为 True 但非绝对。"
            "--no-enable-prefix-caching 由 argparse.BooleanOptionalAction 自动生成"
            "（arg_utils.py:352），源码中搜不到该字面量但确实可用。"
            "归一为'是否启用前缀缓存'再比。"),

    # ── 调度 / 批 ──────────────────────────────────────────────────────
    _p("max_running_requests", kind=K_INT,
       sgl_flags=["--max-running-requests"],
       vllm_flags=["--max-num-seqs"],
       sgl_default=None, sgl_kind=DERIVED,
       sgl_derived_from=["kv_pool_capacity", "mem_fraction_static"],
       vllm_default=128, vllm_kind=STATIC,
       comparable=False,
       note="⚠ 未显式设置时不可比：SGLang 默认 None，按 KV pool 容量推导，"
            "在大显存机器（如 8×H200）上常远高于 128；vLLM 固定默认 128"
            "（DEFAULT_MAX_NUM_SEQS，config/scheduler.py:44）。"),

    _p("chunked_prefill_size", kind=K_INT,
       sgl_flags=["--chunked-prefill-size"],
       vllm_flags=["--max-num-batched-tokens"],
       sgl_default=None, sgl_kind=DERIVED,
       sgl_derived_from=["gpu_memory_capacity"],
       vllm_default=2048, vllm_kind=STATIC,
       comparable=False,
       note="⚠ 未显式设置时不可比：SGLang 默认 None，按显存档位取值"
            "（<20G→2048, <35G→2048, …，_handle_gpu_memory_settings）；"
            "vLLM 固定 DEFAULT_MAX_NUM_BATCHED_TOKENS=2048。"
            "另注意 SGLang 的 max_prefill_tokens(默认 16384) 是另一个独立旋钮，"
            "不要与本项混为一谈。"),

    _p("context_length", kind=K_INT,
       sgl_flags=["--context-length"],
       vllm_flags=["--max-model-len"],
       sgl_default=None, sgl_kind=DERIVED,
       sgl_derived_from=["model_config.max_position_embeddings"],
       vllm_default=None, vllm_kind=DERIVED,
       vllm_derived_from=["model_config.max_position_embeddings"],
       note="两边默认都是 None → 从模型 config 读取，语义一致，可比。"),

    # ── 模型 / 量化 ────────────────────────────────────────────────────
    _p("model_path", kind=K_STR,
       sgl_flags=["--model-path", "--model"],
       vllm_flags=["--model"],
       sgl_default=None, vllm_default=None,
       note="SGLang 唯一的必填字段（无默认值），显式声明 --model 为 alias。"),

    _p("served_model_name", kind=K_STR,
       sgl_flags=["--served-model-name"],
       vllm_flags=["--served-model-name"],
       sgl_default=None, vllm_default=None),

    _p("dtype", kind=K_STR,
       sgl_flags=["--dtype"], vllm_flags=["--dtype"],
       sgl_default="auto", vllm_default="auto"),

    _p("quantization", kind=K_STR,
       sgl_flags=["--quantization"], vllm_flags=["--quantization", "-q"],
       sgl_default=None, vllm_default=None,
       note="取值词表两边不完全一致（如 SGLang 的 modelopt_fp4 vs vLLM 的 modelopt）。"),

    _p("trust_remote_code", kind=K_BOOL,
       sgl_flags=["--trust-remote-code"], vllm_flags=["--trust-remote-code"],
       sgl_default=False, vllm_default=False),

    _p("seed", kind=K_INT,
       sgl_flags=["--random-seed"], vllm_flags=["--seed"],
       sgl_default=None, vllm_default=0,
       comparable=False,
       note="⚠ 默认行为不同：SGLang None（不固定种子）vs vLLM 0（固定种子）。"
            "影响结果可复现性，不宜按数值比较。"),

    # ── 编译 / 图 ──────────────────────────────────────────────────────
    _p("torch_compile", kind=K_INVERTED,
       sgl_flags=["--enable-torch-compile"],
       vllm_flags=["--enforce-eager"],
       sgl_default=False, vllm_default=True,
       note="⚠ 极性相反：SGLang 默认关，需 --enable-torch-compile 打开（opt-in）；"
            "vLLM 默认开，用 --enforce-eager 关闭（opt-out）。"
            "归一为'是否启用编译/CUDA graph'再比。"),

    # ── 注意力后端 ─────────────────────────────────────────────────────
    _p("attention_backend", kind=K_STR,
       sgl_flags=["--attention-backend"],
       vllm_flags=[],
       sgl_default=None, sgl_kind=DERIVED,
       sgl_derived_from=["gpu_arch", "model_arch", "kv_cache_dtype"],
       vllm_default=None, vllm_kind=NA,
       comparable=False,
       note="vLLM 无单一等价 CLI flag：后端由平台自动选择，"
            "只能经环境变量 VLLM_ATTENTION_BACKEND 或 --compilation-config 间接影响，"
            "因此不建立配对，仅在 SGLang 侧单列。"),

    # ── 投机解码 ───────────────────────────────────────────────────────
    # SGLang 是一组扁平 flag；vLLM 收在 --speculative-config 一个 JSON 里。
    _p("spec_algorithm", kind=K_STR,
       sgl_flags=["--speculative-algorithm"],
       vllm_flags=["--speculative-config"],
       sgl_default=None, vllm_default=None,
       note="结构不同：SGLang 用扁平 flag（--speculative-algorithm EAGLE）；"
            "vLLM 用 JSON（--speculative-config '{\"method\":\"mtp\",...}'）。"
            "需从 JSON 的 method 字段取值后再比。"),

    _p("spec_num_steps", kind=K_INT,
       sgl_flags=["--speculative-num-steps"],
       vllm_flags=["--speculative-config"],
       sgl_default=None, vllm_default=None,
       note="vLLM 侧对应 JSON 内的 num_speculative_tokens（语义相近但不完全等同："
            "SGLang 另有 eagle_topk / num_draft_tokens 共同决定草稿树形状）。"),

    _p("spec_eagle_topk", kind=K_INT,
       sgl_flags=["--speculative-eagle-topk"],
       vllm_flags=[],
       sgl_default=None, vllm_default=None, vllm_kind=NA,
       comparable=False,
       note="vLLM 无等价项（其 MTP/EAGLE 实现不暴露 topk）。"),

    _p("spec_num_draft_tokens", kind=K_INT,
       sgl_flags=["--speculative-num-draft-tokens"],
       vllm_flags=["--speculative-config"],
       sgl_default=None, vllm_default=None,
       note="vLLM 侧近似对应 JSON 内 num_speculative_tokens。"),

    # ── MoE ────────────────────────────────────────────────────────────
    _p("moe_a2a_backend", kind=K_STR,
       sgl_flags=["--moe-a2a-backend"],
       vllm_flags=[],
       sgl_default="none", sgl_kind=DERIVED,
       vllm_default=None, vllm_kind=NA,
       comparable=False,
       note="vLLM 无等价 CLI flag（DeepEP 等经 --compilation-config / 环境变量启用）。"),

    _p("dp_attention", kind=K_BOOL,
       sgl_flags=["--enable-dp-attention"],
       vllm_flags=[],
       sgl_default=False, vllm_default=None, vllm_kind=NA,
       comparable=False,
       note="SGLang 专属（DP attention）。vLLM 无对应开关。"),

    # ── 分层缓存 / KV offload ──────────────────────────────────────────
    _p("hicache", kind=K_BOOL,
       sgl_flags=["--enable-hierarchical-cache"],
       vllm_flags=["--kv-offloading-size"],
       sgl_default=False, vllm_default=None,
       comparable=False,
       note="⚠ 非等价，仅粗略近似：SGLang hierarchical cache 是多级 KV 缓存；"
            "vLLM --kv-offloading-size 是 KV 卸载到 CPU 的容量。"
            "机制不同，只能标注'两边都启用了某种 KV 分层/卸载'，不宜数值对比。"),
]

# key → 条目
PARAM_BY_KEY = {p["key"]: p for p in PARAM_PAIRS}


def flags_for(framework: str):
    """框架 → {flag: key}，供解析与 unrecognized 识别复用（不再手工维护白名单）。"""
    out = {}
    for p in PARAM_PAIRS:
        for f in p[framework]["flags"]:
            out[f] = p["key"]
    return out


def known_flags(framework: str) -> set:
    """该框架已被本表覆盖的全部 flag。由表派生，不手工同步。"""
    return set(flags_for(framework))


def comparable_keys() -> list:
    """可安全跨框架直接比较的 key。"""
    return [p["key"] for p in PARAM_PAIRS if p["comparable"]]


def normalize_ep(framework: str, raw):
    """
    EP 归一：两边都返回 (enabled: bool, width: int|None)。

    SGLang ep_size=1 与 vLLM enable_expert_parallel=False 都表示"未开启"，
    归一后一致，避免报告出现假差异。
    """
    if raw is None:
        return (False, None)
    if framework == "sglang":
        try:
            w = int(raw)
        except (TypeError, ValueError):
            return (False, None)
        return (w > 1, w)
    return (bool(raw), None)  # vLLM：只有开关，宽度由 TP/DP 隐式决定
