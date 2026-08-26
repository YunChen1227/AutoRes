"""
模型 config.json → AutoRes 字段映射 + DERIVED 启动参数推导（D23）。

────────────────────────────────────────────────────────────────────────
为什么需要这个模块
────────────────────────────────────────────────────────────────────────
param_map.py 把一批参数标记为 DERIVED：它们在启动命令里通常**不写**，
由 vllm / sglang 在运行时按「模型 config.json + 硬件」推导。只解析启动命令
拿不到这些值，入库后相应列全是 NULL，报告里既看不到实际生效值、也无法判断
两次测试是否真的同配置。

本模块把上游的推导逻辑搬过来：给定 config.json（+ 显卡型号 + 已解析的显式
参数），算出与真实启动一致的生效值，并标注每个值的来源。

────────────────────────────────────────────────────────────────────────
抽取基线 commit（与 param_map.py 同一份源码树）
────────────────────────────────────────────────────────────────────────
  vLLM   385d4c084e
  SGLang c113ead98a

逐项对应的上游位置：
  context_length
      sglang  utils/hf_transformers/common.py:get_context_length
              （CONTEXT_LENGTH_KEYS 取**第一个命中**的键 × rope factor）
      vllm    transformers_utils/model_arch_config_convertor.py
              :derive_max_model_len_and_key（possible_keys 取**最小值**，
              model_max_length 存在时直接覆盖）
              + config/model.py:_get_and_verify_max_len（rope/yarn/longrope）
      ⚠ 两边键清单与聚合方式都不同，必须分开实现，不能共用一套。
  dtype
      sglang  configs/model_config.py:_get_and_verify_dtype
              （auto + float32 → gemma* 用 bfloat16，其余 float16）
      vllm    config/model.py:_resolve_auto_dtype
              （auto + float32 → 平台首选 dtype，现代 CUDA 为 bfloat16）
  quantization
      两边都读 hf_config.quantization_config["quant_method"]
      （sglang configs/model_config.py:1496 / vllm config/model.py:1273）
  chunked_prefill_size ↔ max_num_batched_tokens
      sglang  server_args.py:_handle_gpu_memory_settings（显存分档，
              已在 gpu_memory_presets.py 里搬过一遍，此处直接复用）
      vllm    engine/arg_utils.py:get_batch_defaults
              + _set_default_max_num_seqs_and_batched_tokens_args
  max_running_requests ↔ max_num_seqs
      vllm    同上（min(默认值, max_num_batched_tokens)）
      sglang  由 KV pool 实际容量反推，依赖 mem_fraction_static 的运行时
              取值 —— 拿不到，不编（见下"刻意不推导的参数"）
  page_size ↔ block_size
      sglang  arg_groups/overrides.py:_page_size_default → 1
      vllm    config/cache.py:DEFAULT_BLOCK_SIZE → 16
  prefix_caching
      sglang  默认 True（--disable-radix-cache 关闭）
      vllm    config/model.py:is_prefix_caching_supported
  mem_fraction
      vllm    config/cache.py:gpu_memory_utilization → 0.92（静态字面量）
      sglang  见下

────────────────────────────────────────────────────────────────────────
模型元信息（model_dtype / model_params_b / model_weight_gb）
────────────────────────────────────────────────────────────────────────
这三个不是启动参数，而是 test_runs 的元信息列，原先靠上传表单手填。
config.json 强制上传后改为推导，规则见 §4b / §2b：

  model_dtype     权重精度，不等于框架的计算 dtype。量化 checkpoint 的
                  torch_dtype 往往还是 bfloat16（那是激活/计算精度），
                  真实权重精度要看 quantization_config。故量化块优先、
                  torch_dtype 兜底。quant_method → 精度的映射表按两边
                  上游的注册表逐项对齐：
                    vllm   layers/quantization/__init__.py:QUANTIZATION_METHODS
                    sglang layers/quantization/__init__.py:BASE_QUANTIZATION_METHODS
  model_params_b  参数量（单位 B = 10^9）。由 config 的形状字段逐块累加，
                  不读权重文件。用户仍需填写，推导值只做预填与偏差告警——
                  MoE / 多模态的层布局各家差异太大，不适合当唯一真值。
  model_weight_gb 权重实际占用（GiB）。按「层内线性层走量化精度、
                  embedding/lm_head/vision tower 走 torch_dtype」的常见
                  checkpoint 布局分段计算，而不是拿单一 dtype 乘总参数量。

────────────────────────────────────────────────────────────────────────
刻意不推导的参数（给不出可信值，宁缺勿编）
────────────────────────────────────────────────────────────────────────
  sglang mem_fraction_static
      公式依赖 attention backend 是否 MLA、DP attention 开关、
      moe_a2a_backend、cuda graph buffer 等一串运行时状态
      （详见 gpu_memory_presets.py 顶部说明 2）。
  sglang max_running_requests
      由 KV pool 容量反推，而 KV pool 容量又取决于上面那个 mem_fraction。
  sglang attention_backend
      按 GPU 架构 + 模型架构 + kv_cache_dtype 分派，规则长且随版本变动。
  vllm 多模态 prefix-LM 的 max_num_batched_tokens 抬升
      需要 MULTIMODAL_REGISTRY 真实处理器算每模态 token 上限。
  model_weight_gb（量化方式无法定位到位宽时）
      torchao / gguf / inc / modelslim / MIXED_PRECISION 等按层各不相同，
      config.json 里没有足够信息还原逐层位宽。

这几项在结果里既不写值、也不写来源，report 层看到 NULL 即"未知"。

────────────────────────────────────────────────────────────────────────
已知精度边界
────────────────────────────────────────────────────────────────────────
1. 只吃 config.json。上游还会读 tokenizer_config.json（model_max_length）、
   generation_config.json、hf_quant_config.json 与 safetensors 头
   （config 里没写 dtype 时从权重反查）。这些没上传时，相关推导会退化，
   本模块在 notes 里显式说明，不静默糊过去。
2. 平台相关分支按 **NVIDIA CUDA + `vllm serve`（UsageContext.OPENAI_API_SERVER）**
   取值——这是我们实际的压测形态。昇腾/沐曦/平头哥走各自 platform 实现，
   显存分档只是"容量凑巧落在哪个区间"的参考（同 gpu_memory_presets.py 说明 1）。
3. 显式写在启动命令里的值永远优先，本模块只填命令里没写的。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gpu_memory_presets as gmp  # noqa: E402
import param_map as pm  # noqa: E402

# ── 参数来源标记（写入 extra.param_sources，报告层据此判断"能不能比"）──
SRC_EXPLICIT = "explicit"  # 启动命令里显式写了
SRC_CONFIG = "config"      # 由模型 config.json 推导（上游同逻辑）
SRC_GPU = "gpu"            # 由显卡显存分档推导（上游同逻辑）
SRC_STATIC = "static"      # 上游源码里的静态字面量默认值

# HF config.json 实测都在 20KB 量级；1MB 已是极宽松的上限
MAX_CONFIG_BYTES = 1024 * 1024

# 时间戳目录里存放模型 config 原文的文件名（to_csv 落盘 / upload 落盘 / Scanner 读回共用）。
# 不叫 config.json 是为了不和 AutoRes 自己的服务配置混淆。
RUN_DIR_FILENAME = "model_config.json"

# 多模态 config 的文本子配置字段名。
# 顺序与 sglang utils/hf_transformers/common.py:get_hf_text_config 一致。
_TEXT_SUBCONFIG_KEYS = ("text_config", "llm_config", "language_config", "thinker_config")

# dtype 字符串 → 每元素字节数。名字对齐 torch dtype，别名一并收（老配置常写 fp16）。
_DTYPE_BYTES = {
    "float32": 4, "float": 4, "fp32": 4,
    "float16": 2, "half": 2, "fp16": 2,
    "bfloat16": 2, "bf16": 2,
    "float8_e4m3fn": 1, "float8_e5m2": 1, "fp8": 1, "fp8_e4m3": 1, "fp8_e5m2": 1,
    "int8": 1, "uint8": 1,
}

# dtype 别名归一到 torch 拼法（两边框架的 _STR_DTYPE_TO_TORCH_DTYPE 都只认这几个）
_DTYPE_CANON = {
    "float32": "float32", "float": "float32", "fp32": "float32",
    "float16": "float16", "half": "float16", "fp16": "float16",
    "bfloat16": "bfloat16", "bf16": "bfloat16",
}

# ── model_dtype 列（权重精度）的取值与位宽 ──
#
# 键集合必须与 autores/db/schema.py:MODEL_DTYPE_CHOICES 一致。这里不 import
# 那份定义：tools/ 是能脱离 autores 包单独跑的脚本目录（同 param_map.py 的处置）。
# 值是每元素字节数，4bit 记 0.5。
MODEL_DTYPE_BYTES: dict[str, float] = {
    "bf16": 2, "fp16": 2, "fp8": 1, "int8": 1, "int4": 0.5, "fp4": 0.5,
}

# torch dtype → model_dtype（未量化 checkpoint 走这条）
_TORCH_TO_MODEL_DTYPE = {"bfloat16": "bf16", "float16": "fp16"}

# quantization_config.quant_method → 权重精度。
#
# 键取两边上游注册表的并集（vllm QUANTIZATION_METHODS / sglang
# BASE_QUANTIZATION_METHODS），只收会出现在 checkpoint 的 quant_method 里的名字，
# 不收 `--quantization` 才用的在线量化简写（fp8_per_tensor 等——那些是显式参数，
# 走 derive_quantization 而不是这里）。
# 值为 None = 位宽写在别的字段里，交给 _QUANT_DETAIL_READERS 细分。
_QUANT_METHOD_DTYPE: dict[str, str | None] = {
    # 8bit 浮点
    "fp8": "fp8", "mxfp8": "fp8", "w8a8_fp8": "fp8", "fbgemm_fp8": "fp8",
    "deepseek_v4_fp8": "fp8", "modelopt_fp8": "fp8", "modelopt_mxfp8": "fp8",
    # 8bit 整型
    "blockwise_int8": "int8", "w8a8_int8": "int8", "experts_int8": "int8",
    "auto-round-int8": "int8", "mlx_q8": "int8",
    # 4bit 浮点
    "mxfp4": "fp4", "gpt_oss_mxfp4": "fp4", "quark_mxfp4": "fp4",
    "petit_nvfp4": "fp4", "modelopt_fp4": "fp4", "mxfp_w4a8": "fp4",
    # 4bit 整型
    "w4afp8": "int4", "quark_int4fp8_moe": "int4", "mlx_q4": "int4",
    # 位宽在别处
    "modelopt": None,             # quant_algo
    "quark": None,                # quant_algo
    "compressed-tensors": None,   # config_groups[].weights.{num_bits,type}
    "awq": None, "awq_marlin": None, "auto_awq": None,          # bits
    "gptq": None, "gptq_marlin": None, "auto_gptq": None,       # bits
    "moe_wna16": None, "auto-round": None,                      # bits
    "bitsandbytes": None,         # load_in_4bit / load_in_8bit
    # 逐层位宽不同或 config 里给不出，明确不推（见"刻意不推导"）
    "modelopt_mixed": None, "torchao": None, "gguf": None,
    "inc": None, "modelslim": None, "humming": None, "fp_quant": None,
}

# modelopt / quark 的 quant_algo → 权重精度
# （sglang modelopt_quant.py 的分支取值：FP8 / MXFP8 / NVFP4 / W4A16_NVFP4 /
#   MIXED_PRECISION；TensorRT-LLM 侧还会出 INT8_SQ / INT4_AWQ / W4A8_AWQ）
_QUANT_ALGO_DTYPE = {
    "fp8": "fp8", "mxfp8": "fp8",
    "nvfp4": "fp4", "w4a16_nvfp4": "fp4",
    "int8_sq": "int8", "int8": "int8",
    "int4_awq": "int4", "w4a8_awq": "int4", "int4": "int4",
}


class ModelConfigError(ValueError):
    """config.json 缺失/非法/不像 HF 模型配置。上层转成用户可修正的报错。"""


# ════════════════════════════════════════════════════════════════════════
# 1. 读取与取值辅助
# ════════════════════════════════════════════════════════════════════════

def load_config(raw) -> dict:
    """
    bytes / str / dict → config dict。

    校验到"像 HF 模型 config.json"为止（有 architectures 或 model_type），
    避免用户误传 tokenizer_config.json / generation_config.json 之类。
    """
    if isinstance(raw, dict):
        cfg = raw
    else:
        if isinstance(raw, bytes):
            if len(raw) > MAX_CONFIG_BYTES:
                raise ModelConfigError(
                    f"config.json 超过大小上限（{MAX_CONFIG_BYTES // 1024} KB）")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as e:
                raise ModelConfigError("config.json 编码无法识别（请用 UTF-8 保存）") from e
        else:
            text = str(raw)
        if not text.strip():
            raise ModelConfigError("config.json 为空")
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError as e:
            raise ModelConfigError(f"config.json 不是合法 JSON：{e}") from e

    if not isinstance(cfg, dict):
        raise ModelConfigError("config.json 顶层必须是 JSON 对象")
    if not cfg.get("architectures") and not cfg.get("model_type"):
        raise ModelConfigError(
            "config.json 里没有 architectures / model_type，不像 HF 模型配置文件。"
            "请上传模型目录下的 config.json（不是 tokenizer_config.json / "
            "generation_config.json）。")
    return cfg


def text_config(cfg: dict) -> dict:
    """
    多模态 config 的文本子配置（对应上游 hf_text_config）；纯文本模型返回自身。
    模型形状与长度类字段都应从这里读——多模态顶层放的是 vision 侧的数。
    """
    for key in _TEXT_SUBCONFIG_KEYS:
        sub = cfg.get(key)
        if isinstance(sub, dict):
            return sub
    return cfg


def _rope_params(node: dict) -> dict:
    """rope 配置块。transformers v5 改名 rope_parameters，老配置仍是 rope_scaling。"""
    for key in ("rope_parameters", "rope_scaling"):
        val = node.get(key)
        if isinstance(val, dict) and val:
            return val
    return {}


def _first_int(node: dict, *keys):
    """按给定顺序取第一个可转 int 的键值（缺失/None/非数值都跳过）。"""
    for key in keys:
        val = node.get(key)
        if val is None:
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def _first_float(node: dict, *keys):
    """_first_int 的 float 版，给 mlp_ratio 这类非整数字段用。"""
    for key in keys:
        val = node.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _vision_dims(vision: dict) -> tuple[int | None, int | None]:
    """
    区分 vision tower 的「内部宽度」和「输出宽度」，两代 Qwen-VL 的键名是反的：

    - Qwen2.5-VL 系：hidden_size=1280 是内部宽度，out_hidden_size=3584 是输出宽度。
    - Qwen2-VL 系：embed_dim=1280 才是内部宽度，hidden_size=3584 是输出宽度。

    所以判据是「谁存在」而不是「谁优先」：出现 out_hidden_size 就按 2.5 代读，
    出现 embed_dim 就按 2 代读。CLIP / SigLIP 只有 hidden_size，输出宽度未知
    （projector 形状不在 vision_config 里），返回 None 让调用方跳过 merger 项。
    """
    if not vision:
        return None, None
    out_hidden = _first_int(vision, "out_hidden_size")
    if out_hidden is not None:
        return _first_int(vision, "hidden_size"), out_hidden
    embed_dim = _first_int(vision, "embed_dim")
    if embed_dim is not None:
        return embed_dim, _first_int(vision, "hidden_size")
    return _first_int(vision, "hidden_size"), None


def _canon_dtype(val) -> str | None:
    """config 里的 dtype 字符串 → torch 拼法；无法识别返回原值小写。"""
    if not isinstance(val, str) or not val.strip():
        return None
    s = val.strip().lower()
    return _DTYPE_CANON.get(s, s)


def _config_dtype(cfg: dict) -> str | None:
    """
    config 声明的权重 dtype。

    transformers v5 把 torch_dtype 改名为 dtype，两个键都要认；多模态模型顶层
    可能没写，按 vllm ModelConfigConvertor.get_torch_dtype 的顺序回退到
    text_config / vision_config / encoder_config。
    """
    for node in (cfg, text_config(cfg), cfg.get("vision_config"), cfg.get("encoder_config")):
        if not isinstance(node, dict):
            continue
        got = _canon_dtype(node.get("dtype")) or _canon_dtype(node.get("torch_dtype"))
        if got:
            return got
    return None


def dtype_bytes(dtype: str | None, default: int = 2) -> int:
    """dtype 名 → 每元素字节数；未知时按 default（16bit）算。"""
    if not dtype:
        return default
    return _DTYPE_BYTES.get(str(dtype).strip().lower(), default)


# ════════════════════════════════════════════════════════════════════════
# 2. config.json → 我们自己的模型结构字段（model_arch）
# ════════════════════════════════════════════════════════════════════════
#
# 字段名一律用 AutoRes 自己的拼法，不跟着 HF 各家模型的键名走
# （同一个概念在不同模型里能有三四种键名，见各字段的候选键清单）。

def normalize(cfg: dict) -> dict:
    """
    HF config.json → AutoRes model_arch 字段。

    只做"读 + 归一"，不做任何依赖硬件/启动参数的推导（那部分在 resolve()）。
    取不到的字段一律为 None，不填猜测值。
    """
    tc = text_config(cfg)
    rope = _rope_params(tc)
    quant = cfg.get("quantization_config")
    if not isinstance(quant, dict):
        quant = tc.get("quantization_config")
    if not isinstance(quant, dict):
        quant = {}

    archs = cfg.get("architectures") or []
    arch = archs[0] if isinstance(archs, list) and archs else None

    num_attn_heads = _first_int(tc, "num_attention_heads", "n_head", "num_heads")
    hidden_size = _first_int(tc, "hidden_size", "n_embd", "d_model")
    num_kv_heads = _first_int(tc, "num_key_value_heads", "num_kv_heads",
                              "multi_query_group_num", "n_head_kv")
    if num_kv_heads is None:
        num_kv_heads = num_attn_heads  # 未声明 GQA → MHA，kv 头数等于注意力头数

    head_dim = _first_int(tc, "head_dim", "attention_head_dim")
    if head_dim is None and hidden_size and num_attn_heads:
        head_dim = hidden_size // num_attn_heads

    # MoE：各家键名差异很大，收全常见拼法
    num_experts = _first_int(tc, "num_experts", "n_routed_experts",
                             "num_local_experts", "moe_num_experts", "num_experts_per_layer")

    # MoE 层内宽度：专家的 FFN 宽度通常远小于稠密层的 intermediate_size，
    # 参数量估算必须用这个而不是 intermediate_size，否则会高估好几倍。
    moe_intermediate_size = _first_int(tc, "moe_intermediate_size",
                                       "expert_intermediate_size", "moe_ffn_hidden_size")

    # MLA（DeepSeek 系）：有 kv_lora_rank 即走 MLA，KV 占用公式与常规 GQA 不同
    kv_lora_rank = _first_int(tc, "kv_lora_rank")

    vision = cfg.get("vision_config")
    vision = vision if isinstance(vision, dict) else {}
    vision_hidden, vision_out = _vision_dims(vision)

    sliding_window = _first_int(tc, "sliding_window_size", "sliding_window", "window_size")
    if sliding_window is not None and sliding_window <= 0:
        sliding_window = None  # 0 = 显式声明"不用滑窗"（同 sglang is_hybrid_swa_model 判定）

    rope_factor = rope.get("factor")
    try:
        rope_factor = float(rope_factor) if rope_factor is not None else None
    except (TypeError, ValueError):
        rope_factor = None

    return {
        # 身份
        "arch": arch,
        "model_type": cfg.get("model_type") or tc.get("model_type"),
        "text_model_type": tc.get("model_type"),
        # 形状（KV / 权重占用的计算基础）
        "num_layers": _first_int(tc, "num_hidden_layers", "n_layer", "num_layers"),
        "hidden_size": hidden_size,
        "intermediate_size": _first_int(tc, "intermediate_size", "ffn_hidden_size"),
        "num_attn_heads": num_attn_heads,
        "num_kv_heads": num_kv_heads,
        "head_dim": head_dim,
        "vocab_size": _first_int(tc, "vocab_size", "padded_vocab_size"),
        "tie_word_embeddings": bool(tc.get("tie_word_embeddings", False)),
        # 上下文长度（context_length / max_model_len 的推导输入）
        "max_position_embeddings": _first_int(tc, "max_position_embeddings"),
        "sliding_window": sliding_window,
        "rope_type": rope.get("rope_type") or rope.get("type"),
        "rope_factor": rope_factor,
        "rope_original_max_position_embeddings":
            _first_int(rope, "original_max_position_embeddings"),
        # dtype / 量化
        "config_dtype": _config_dtype(cfg),
        "quant_method": (quant.get("quant_method") or "").strip().lower() or None,
        "quant_algo": quant.get("quant_algo") or quant.get("algorithm"),
        "kv_cache_quant_algo": quant.get("kv_cache_quant_algo"),
        # MoE
        "is_moe": num_experts is not None and num_experts > 1,
        "num_experts": num_experts,
        "num_experts_per_tok": _first_int(tc, "num_experts_per_tok", "moe_topk",
                                          "num_experts_per_token"),
        "moe_intermediate_size": moe_intermediate_size,
        "num_shared_experts": _first_int(tc, "n_shared_experts", "num_shared_experts"),
        "shared_expert_intermediate_size":
            _first_int(tc, "shared_expert_intermediate_size", "moe_shared_expert_intermediate_size"),
        # DeepSeek 系前 N 层是稠密的（走 intermediate_size 而非 moe_intermediate_size）
        "first_k_dense_layers": _first_int(tc, "first_k_dense_replace"),
        # Qwen2-MoE 系每隔 N 层才是 MoE 层；moe_layer_freq 有时是逐层列表，
        # _first_int 对列表取不到值 → None，此时按"全是 MoE 层"处理并在 notes 里说明。
        "moe_layer_step": _first_int(tc, "decoder_sparse_step", "moe_layer_freq"),
        # MTP / Eagle 草稿层也占权重（DeepSeek-V3 有 1 层）
        "num_nextn_predict_layers": _first_int(tc, "num_nextn_predict_layers"),
        # MLA
        "is_mla": kv_lora_rank is not None,
        "kv_lora_rank": kv_lora_rank,
        "q_lora_rank": _first_int(tc, "q_lora_rank"),
        "qk_rope_head_dim": _first_int(tc, "qk_rope_head_dim"),
        "qk_nope_head_dim": _first_int(tc, "qk_nope_head_dim"),
        "v_head_dim": _first_int(tc, "v_head_dim"),
        # 多模态
        "is_multimodal": bool(vision),
        "vision_model_type": vision.get("model_type"),
        "vision_num_layers": _first_int(vision, "num_hidden_layers", "depth"),
        "vision_hidden_size": vision_hidden,
        "vision_out_hidden_size": vision_out,
        "vision_intermediate_size": _first_int(vision, "intermediate_size"),
        "vision_mlp_ratio": _first_float(vision, "mlp_ratio"),
        "vision_hidden_act": vision.get("hidden_act") or vision.get("hidden_activation"),
        "vision_num_heads": _first_int(vision, "num_attention_heads", "num_heads"),
        # patch embedding / merger 的形状（参数量估算要算这两块）
        "vision_patch_size": _first_int(vision, "patch_size"),
        "vision_temporal_patch_size": _first_int(vision, "temporal_patch_size"),
        "vision_in_channels": _first_int(vision, "in_channels", "in_chans", "num_channels"),
        "vision_spatial_merge_size": _first_int(vision, "spatial_merge_size"),
        # 量化细节（权重精度与权重占用的推导输入，见 §4b）
        "quant_bits": _first_int(quant, "bits", "weight_bits", "w_bit"),
        "quant_config_groups": quant.get("config_groups") if isinstance(
            quant.get("config_groups"), dict) else None,
        "quant_load_in_4bit": bool(quant.get("load_in_4bit")),
        "quant_load_in_8bit": bool(quant.get("load_in_8bit")),
        # 量化时被跳过的模块（embedding / lm_head / router 等），键名各家不同
        "quant_ignored": _quant_ignored(quant),
    }


def _quant_ignored(quant: dict) -> list[str]:
    """
    量化时保持原精度的模块名列表。

    键名各家不同：compressed-tensors 用 ignore，modelopt 用 exclude_modules，
    bitsandbytes / awq 用 modules_to_not_convert，fp8 用 ignored_modules。
    """
    for key in ("ignore", "ignored_modules", "exclude_modules", "modules_to_not_convert"):
        val = quant.get(key)
        if isinstance(val, list):
            return [str(v) for v in val]
    return []


def kv_bytes_per_token(arch: dict, kv_dtype_bytes: int = 2) -> int | None:
    """
    单 token 全层 KV 占用（字节，未按 TP 切分）。

    常规 attention : 2(K+V) × num_layers × num_kv_heads × head_dim × 字节数
    MLA（DeepSeek）: num_layers × (kv_lora_rank + qk_rope_head_dim) × 字节数
                     —— MLA 只缓存压缩后的 latent + rope 部分，没有 K/V 两份。
    滑窗模型给出的是"满上下文"上限，实际会被 sliding_window 截断，故偏保守。
    形状字段缺失时返回 None，不猜。
    """
    layers = arch.get("num_layers")
    if not layers:
        return None
    if arch.get("is_mla"):
        lora, rope_dim = arch.get("kv_lora_rank"), arch.get("qk_rope_head_dim")
        if not lora or not rope_dim:
            return None
        return layers * (lora + rope_dim) * kv_dtype_bytes
    heads, dim = arch.get("num_kv_heads"), arch.get("head_dim")
    if not heads or not dim:
        return None
    return 2 * layers * heads * dim * kv_dtype_bytes


# ════════════════════════════════════════════════════════════════════════
# 2b. 参数量估算（model_params_b / model_weight_gb 的计算基础）
# ════════════════════════════════════════════════════════════════════════
#
# 只吃 config.json 的形状字段，不读权重文件。分块返回（embedding / layers /
# vision）而不是只给总数，因为量化 checkpoint 各块精度不同，weight_gb 要分段乘。
#
# 精度边界（都会写进 notes，不静默糊过去）：
#   * norm / bias / rope 缓存等小张量不计（合计 <0.1%，稠密模型实测误差在 1% 内）；
#   * MoE 层布局按 first_k_dense_replace + decoder_sparse_step 还原，
#     逐层列表形式的 moe_layer_freq 还原不了，按"全是 MoE 层"处理并告警；
#   * vision tower 按标准 ViT block（非 gated MLP）估，projector / merger 不计——
#     Qwen-VL 系新版的 vision MLP 是 gated 的，这里会偏小；
#   * MTP / Eagle 草稿层按"一层同类型 block + eh_proj"估。

def _attn_params(arch: dict) -> int | None:
    """单层注意力的参数量（q/k/v/o 投影；MLA 走低秩分解那套矩阵）。"""
    hidden = arch.get("hidden_size")
    n_heads = arch.get("num_attn_heads")
    if not hidden or not n_heads:
        return None

    if arch.get("is_mla"):
        kv_lora = arch.get("kv_lora_rank")
        qk_rope = arch.get("qk_rope_head_dim")
        qk_nope = arch.get("qk_nope_head_dim")
        v_dim = arch.get("v_head_dim")
        if not all((kv_lora, qk_rope, qk_nope, v_dim)):
            return None
        q_lora = arch.get("q_lora_rank")
        qk_head = qk_nope + qk_rope
        # q 侧：有 q_lora_rank 走 down/up 两段低秩，否则一段全连接
        q = (hidden * q_lora + q_lora * n_heads * qk_head) if q_lora \
            else hidden * n_heads * qk_head
        kv_a = hidden * (kv_lora + qk_rope)
        kv_b = kv_lora * n_heads * (qk_nope + v_dim)
        o = n_heads * v_dim * hidden
        return q + kv_a + kv_b + o

    head_dim = arch.get("head_dim")
    n_kv = arch.get("num_kv_heads")
    if not head_dim or not n_kv:
        return None
    q = hidden * n_heads * head_dim
    kv = 2 * hidden * n_kv * head_dim
    o = n_heads * head_dim * hidden
    return q + kv + o


def _gated_mlp_params(hidden: int, intermediate: int) -> int:
    """gated SwiGLU 的 gate + up + down 三个矩阵。现代 LLM 一律是这个结构。"""
    return 3 * hidden * intermediate


def _moe_layer_count(arch: dict) -> tuple[int, int, list[str]]:
    """
    MoE 模型的层布局 → (稠密层数, MoE 层数, 告警)。

    两个来源：
      first_k_dense_replace  DeepSeek 系前 N 层强制稠密；
      decoder_sparse_step    Qwen2-MoE 系每 N 层才有一个 MoE 层
                             （判定式 (idx+1) % step == 0，step=1 即全是 MoE）。
    """
    total = arch.get("num_layers") or 0
    notes: list[str] = []
    dense_head = min(arch.get("first_k_dense_layers") or 0, total)
    step = arch.get("moe_layer_step") or 1
    if step < 1:
        step = 1
    moe = sum(1 for i in range(dense_head, total) if (i + 1) % step == 0)
    # first_k_dense_replace 只出现在"其余层全是 MoE"那类布局里，声明了就不必再警告。
    if (arch.get("moe_layer_step") is None
            and arch.get("first_k_dense_layers") is None):
        notes.append(
            "config 未声明 decoder_sparse_step / moe_layer_freq / first_k_dense_replace"
            "（或 moe_layer_freq 是逐层列表），参数量估算按「每层都是 MoE 层」处理；"
            "实际是稠密层与 MoE 层交替时会高估。")
    return total - moe, moe, notes


# vision MLP 用这些激活时是 gated 的（gate/up/down 三个矩阵），否则是 fc1/fc2 两个。
# Qwen2.5-VL=silu（gated），Qwen2-VL / CLIP=quick_gelu、SigLIP=gelu_pytorch_tanh（非 gated）。
_GATED_ACTS = {"silu", "swish", "geglu", "swiglu", "gated_silu"}


def _vision_params(arch: dict) -> tuple[int, list[str]]:
    """
    vision tower 参数量：transformer blocks + patch embedding + patch merger。

    blocks 按 attn 四个方阵（qkv 3h² + proj h²）加 MLP 估算，MLP 是否 gated 由
    hidden_act 判断。patch embedding 是个 Conv3d(in_ch → h, kernel=(t,p,p))。
    merger 只在 spatial_merge_size 和输出宽度都已知时计入（Qwen-VL 系），
    CLIP / SigLIP 那种 projector 形状不在 vision_config 里，算不了就说明算不了。

    取不到 layers / hidden 时返回 0 并给出偏小告警。
    """
    layers = arch.get("vision_num_layers")
    hidden = arch.get("vision_hidden_size")
    if not layers or not hidden:
        if arch.get("is_multimodal"):
            return 0, ["vision_config 缺 num_hidden_layers / hidden_size，"
                       "参数量估算未计入 vision tower，会偏小。"]
        return 0, []

    notes: list[str] = []
    inter = arch.get("vision_intermediate_size")
    if inter is None:
        ratio = arch.get("vision_mlp_ratio")
        inter = int(hidden * ratio) if ratio else hidden * 4
        if not ratio:
            notes.append("vision_config 缺 intermediate_size / mlp_ratio，"
                         "vision MLP 按 4x hidden 估算。")

    act = (arch.get("vision_hidden_act") or "").lower()
    mlp_mats = 3 if act in _GATED_ACTS else 2
    total = layers * (4 * hidden * hidden + mlp_mats * hidden * inter)

    patch = arch.get("vision_patch_size")
    if patch:
        in_ch = arch.get("vision_in_channels") or 3
        temporal = arch.get("vision_temporal_patch_size") or 1
        total += in_ch * hidden * temporal * patch * patch

    merge = arch.get("vision_spatial_merge_size")
    out_hidden = arch.get("vision_out_hidden_size")
    if merge and out_hidden:
        # Qwen-VL 的 merger：Linear(mh→mh) + Linear(mh→out)，mh = hidden * merge²
        mh = hidden * merge * merge
        total += mh * mh + mh + mh * out_hidden + out_hidden
    else:
        notes.append("vision projector / merger 形状未在 vision_config 中声明，"
                     "参数量估算未计入该模块（通常为几十 M 量级）。")
    return total, notes


def estimate_params(arch: dict) -> dict | None:
    """
    config 形状字段 → 分块参数量。

    返回 {"embedding", "layers", "vision", "total", "notes"}；
    核心形状字段（层数 / hidden_size / vocab_size）缺失时返回 None，不猜。
    """
    layers = arch.get("num_layers")
    hidden = arch.get("hidden_size")
    vocab = arch.get("vocab_size")
    if not layers or not hidden or not vocab:
        return None

    attn = _attn_params(arch)
    if attn is None:
        return None

    notes: list[str] = []
    dense_inter = arch.get("intermediate_size")

    if arch.get("is_moe"):
        # Mixtral / gpt-oss 系不写 moe_intermediate_size，专家直接用 intermediate_size；
        # DeepSeek / Qwen-MoE 系两个都写，专家宽度小得多，必须用前者。
        moe_inter = arch.get("moe_intermediate_size") or dense_inter
        n_experts = arch.get("num_experts")
        if not moe_inter or not n_experts:
            return None
        dense_n, moe_n, layout_notes = _moe_layer_count(arch)
        notes.extend(layout_notes)
        if dense_n and not dense_inter:
            return None
        routed = n_experts * _gated_mlp_params(hidden, moe_inter)
        router = hidden * n_experts
        shared_inter = arch.get("shared_expert_intermediate_size") or moe_inter
        shared = (arch.get("num_shared_experts") or 0) \
            * _gated_mlp_params(hidden, shared_inter)
        moe_mlp = routed + router + shared
        dense_mlp = _gated_mlp_params(hidden, dense_inter) if dense_n else 0
        mlp_total = moe_n * moe_mlp + dense_n * dense_mlp
        last_layer = attn + moe_mlp if moe_n else attn + dense_mlp
    else:
        if not dense_inter:
            return None
        mlp_total = layers * _gated_mlp_params(hidden, dense_inter)
        last_layer = attn + _gated_mlp_params(hidden, dense_inter)

    layer_params = layers * attn + mlp_total

    # MTP / Eagle 草稿层：一层同类型 block + eh_proj(2h → h)
    mtp = arch.get("num_nextn_predict_layers") or 0
    if mtp:
        layer_params += mtp * (last_layer + 2 * hidden * hidden)
        notes.append(
            f"计入 {mtp} 层 MTP / Eagle 草稿层（按「一层同类型 block + eh_proj」估）；"
            "不加载草稿层时实际权重更小。")

    embedding = vocab * hidden
    if not arch.get("tie_word_embeddings"):
        embedding += vocab * hidden

    vision, vision_notes = _vision_params(arch)
    notes.extend(vision_notes)

    return {
        "embedding": embedding,
        "layers": layer_params,
        "vision": vision,
        "total": embedding + layer_params + vision,
        "notes": notes,
    }


def estimate_weight_gib(arch: dict, model_dtype: str | None) -> tuple[float | None, list[str]]:
    """
    权重实际占用（GiB，与 gpu_memory_presets.GPU_MEMORY_GIB 同单位，便于直接比显存）。

    量化 checkpoint 的常见布局：层内线性层走量化精度，embedding / lm_head /
    vision tower 保持 torch_dtype（正是 quant_ignored 里最常出现的那几项）。
    故分段乘而不是拿单一精度乘总参数量——后者对 fp8/int4 模型会明显偏小。

    model_dtype 认不出位宽（torchao / gguf / MIXED_PRECISION 等）时返回 None。
    """
    notes: list[str] = []
    weight_bytes = MODEL_DTYPE_BYTES.get(model_dtype or "")
    if weight_bytes is None:
        return None, ["权重精度未能定位到位宽，不估算 model_weight_gb（列留空）。"]

    parts = estimate_params(arch)
    if parts is None:
        return None, ["config 缺核心形状字段（层数 / hidden_size / vocab_size / "
                      "MoE 专家宽度），不估算 model_weight_gb（列留空）。"]
    notes.extend(parts["notes"])

    if model_dtype in _TORCH_TO_MODEL_DTYPE.values():
        total_bytes = parts["total"] * weight_bytes
    else:
        base_bytes = dtype_bytes(arch.get("config_dtype"))
        total_bytes = (parts["layers"] * weight_bytes
                       + (parts["embedding"] + parts["vision"]) * base_bytes)
        notes.append(
            f"权重占用按分段精度估算：层内线性层 {model_dtype}（{weight_bytes}B/元素），"
            f"embedding / lm_head / vision tower 按 config.dtype "
            f"{arch.get('config_dtype')}（{base_bytes}B/元素）。"
            "实际 checkpoint 的 ignore 清单与此不同时会有偏差。")

    return round(total_bytes / (1024 ** 3), 2), notes


# ════════════════════════════════════════════════════════════════════════
# 3. 上下文长度推导（两边键清单与聚合方式不同，必须分开实现）
# ════════════════════════════════════════════════════════════════════════

# sglang utils/hf_transformers/common.py:CONTEXT_LENGTH_KEYS —— 取第一个命中的
_SGL_CONTEXT_KEYS = (
    "max_sequence_length", "seq_length", "max_seq_len",
    "model_max_length", "max_position_embeddings",
)

# vllm model_arch_config_convertor.py:derive_max_model_len_and_key —— 取最小值
_VLLM_CONTEXT_KEYS = (
    "max_position_embeddings", "n_positions", "max_seq_len", "seq_length",
    "model_max_length", "max_target_positions", "max_sequence_length",
    "max_seq_length", "seq_len",
)

# 两边同一个兜底值：config 里一个长度键都没有时用 2048
_CONTEXT_FALLBACK = 2048


def _sglang_context_length(cfg: dict) -> tuple[int, str]:
    """照搬 sglang get_context_length：rope factor × 第一个命中的长度键。"""
    tc = text_config(cfg)
    rope = _rope_params(tc)
    factor = 1.0
    if rope:
        try:
            factor = float(rope.get("factor", 1) or 1)
        except (TypeError, ValueError):
            factor = 1.0
        # 这两种 rope 的 factor 不代表可用长度倍数，上游显式复位为 1
        if "original_max_position_embeddings" in rope:
            factor = 1.0
        if (rope.get("rope_type") or rope.get("type")) == "llama3":
            factor = 1.0

    for key in _SGL_CONTEXT_KEYS:
        val = _first_int(tc, key)
        if val is not None:
            return int(factor * val), key
    return _CONTEXT_FALLBACK, "fallback"


def _vllm_context_length(cfg: dict) -> tuple[int, str]:
    """
    照搬 vllm derive_max_model_len_and_key + _get_and_verify_max_len 的默认分支。

    与 sglang 的三处关键差异：
      1. 候选键取**最小值**（sglang 取第一个命中）；
      2. model_max_length 存在时无条件覆盖（Command-R / Cohere 特例）；
      3. yarn 用 original_max_position_embeddings 作为基数再乘 factor；
         longrope 则直接回落到 original_max_position_embeddings。
    """
    tc = text_config(cfg)
    derived = None
    key_used = None
    for key in _VLLM_CONTEXT_KEYS:
        val = _first_int(tc, key)
        if val is None:
            continue
        if derived is None or val < derived:
            derived, key_used = val, key
    if (mml := _first_int(tc, "model_max_length")) is not None:
        derived, key_used = mml, "model_max_length"
    if derived is None:
        return _CONTEXT_FALLBACK, "fallback"

    rope = _rope_params(tc)
    rope_type = rope.get("rope_type") or rope.get("type")
    if rope and rope_type not in (None, "default", "mrope"):
        if rope_type == "longrope":
            orig = _first_int(rope, "original_max_position_embeddings") \
                or _first_int(tc, "original_max_position_embeddings")
            if orig:
                return orig, "rope:longrope"
        if rope_type == "yarn":
            orig = _first_int(rope, "original_max_position_embeddings")
            if orig:
                derived, key_used = orig, "rope:yarn.original_max_position_embeddings"
        try:
            factor = float(rope.get("factor", 1.0) or 1.0)
        except (TypeError, ValueError):
            factor = 1.0
        derived = int(derived * factor)
    return int(derived), key_used or "fallback"


def derive_context_length(framework: str, cfg: dict) -> tuple[int, str]:
    """
    模型天然支持的上下文长度上限 → (长度, 命中的 config 键名)。

    键名一并返回，写进 notes 便于事后核对"这个数是从哪个字段来的"。
    """
    if framework == "sglang":
        return _sglang_context_length(cfg)
    return _vllm_context_length(cfg)


# ════════════════════════════════════════════════════════════════════════
# 4. dtype / quantization
# ════════════════════════════════════════════════════════════════════════

def derive_dtype(framework: str, cfg: dict, explicit: str | None = None) -> str | None:
    """
    生效的权重 dtype。

    explicit 非 auto → 直接归一后返回；auto / 未写 → 按上游 auto 分支从 config 推：
      sglang : float32 → gemma* 用 bfloat16，其余 float16（_get_and_verify_dtype）
      vllm   : float32 → 平台首选 dtype（现代 CUDA = bfloat16，_resolve_auto_dtype）
    非 float32 时两边一致：直接用 config 声明的 dtype。
    """
    if explicit and str(explicit).strip().lower() != "auto":
        return _canon_dtype(explicit)

    config_dtype = _config_dtype(cfg) or "float32"
    if config_dtype != "float32":
        return config_dtype

    model_type = str(cfg.get("model_type") or text_config(cfg).get("model_type") or "")
    if framework == "sglang":
        return "bfloat16" if model_type.startswith("gemma") else "float16"
    return "bfloat16"


def _quant_block(cfg: dict) -> dict:
    """量化配置块（顶层优先，多模态回落到 text_config）；没有则空 dict。"""
    quant = cfg.get("quantization_config")
    if not isinstance(quant, dict):
        quant = text_config(cfg).get("quantization_config")
    return quant if isinstance(quant, dict) else {}


def derive_quantization(cfg: dict, explicit: str | None = None) -> str | None:
    """
    生效的量化方式。

    显式 --quantization 优先（上游允许显式值覆盖/在线量化）；否则读
    config.quantization_config.quant_method（两边框架都是这个字段）。
    sglang 对 modelopt 还会按 quant_algo 细分成 modelopt_fp8/fp4/w4afp8，
    这里保留 quant_method 原值，细分结果放 model_arch.quant_algo 供追溯。
    """
    if explicit:
        return str(explicit).strip().lower()
    method = _quant_block(cfg).get("quant_method")
    return str(method).strip().lower() if method else None


# ════════════════════════════════════════════════════════════════════════
# 4b. 权重精度（test_runs.model_dtype 列）
# ════════════════════════════════════════════════════════════════════════

def _bits_to_model_dtype(bits: int | None, is_float: bool = False) -> str | None:
    """位宽 (+ 是否浮点) → model_dtype 取值。"""
    if bits == 8:
        return "fp8" if is_float else "int8"
    if bits == 4:
        return "fp4" if is_float else "int4"
    return None


def _compressed_tensors_dtype(groups: dict) -> tuple[str | None, str | None]:
    """
    compressed-tensors 的 config_groups[].weights.{num_bits,type} → (精度, 告警)。

    各 group 位宽不一致时返回 (None, 原因)——逐层混合精度给不出单一取值。
    """
    seen = set()
    for group in groups.values():
        weights = group.get("weights") if isinstance(group, dict) else None
        if not isinstance(weights, dict):
            continue
        bits = weights.get("num_bits")
        is_float = str(weights.get("type") or "").strip().lower() == "float"
        got = _bits_to_model_dtype(bits if isinstance(bits, int) else None, is_float)
        if got:
            seen.add(got)
    if len(seen) == 1:
        return seen.pop(), None
    if len(seen) > 1:
        return None, (f"compressed-tensors 的 config_groups 位宽不一致（{sorted(seen)}），"
                      "无法归到单一权重精度。")
    return None, "compressed-tensors 的 config_groups 里读不到 weights.num_bits。"


def derive_model_dtype(
    cfg: dict,
    arch: dict | None = None,
    effective_dtype: str | None = None,
) -> tuple[str | None, list[str]]:
    """
    权重精度 → MODEL_DTYPE_BYTES 的键（test_runs.model_dtype 列）。

    量化块优先、计算 dtype 兜底：量化 checkpoint 的 torch_dtype 是激活/计算精度
    （多为 bfloat16），不是权重精度。DeepSeek-V3 就是 torch_dtype=bfloat16 +
    quant_method=fp8，只看前者会把它记成 bf16。

    effective_dtype 传 derive_dtype() 的结果（框架 auto 分支解析后的 torch dtype）；
    不传则回落到 config 声明的 dtype。

    返回 (精度 | None, 告警列表)。认不出量化方式时返回 None——宁缺勿编。
    """
    arch = arch if arch is not None else normalize(cfg)
    notes: list[str] = []

    base = _TORCH_TO_MODEL_DTYPE.get(
        _canon_dtype(effective_dtype) or arch.get("config_dtype") or "")

    method = arch.get("quant_method")
    if not method:
        if base is None:
            notes.append(
                f"config 声明的 dtype 是 {arch.get('config_dtype')!r}，"
                "不在 bf16 / fp16 之内，model_dtype 留空。")
        return base, notes

    if method not in _QUANT_METHOD_DTYPE:
        return None, [f"未收录的量化方式 quant_method={method!r}，model_dtype 留空。"
                      "如需支持请在 model_config.py:_QUANT_METHOD_DTYPE 补一行。"]

    mapped = _QUANT_METHOD_DTYPE[method]
    if mapped:
        return mapped, notes

    # 位宽写在别的字段里，按量化方式分头读
    if method in ("modelopt", "quark"):
        algo = str(arch.get("quant_algo") or "").strip().lower()
        got = _QUANT_ALGO_DTYPE.get(algo)
        if got:
            return got, notes
        return None, [f"{method} 的 quant_algo={arch.get('quant_algo')!r} 无法归到单一"
                      "权重精度（MIXED_PRECISION 等逐层不同），model_dtype 留空。"]

    if method == "compressed-tensors":
        groups = arch.get("quant_config_groups")
        if isinstance(groups, dict):
            got, why = _compressed_tensors_dtype(groups)
            if got:
                return got, notes
            return None, [why or "compressed-tensors 位宽读不出，model_dtype 留空。"]
        return None, ["compressed-tensors 的 quantization_config 里没有 config_groups，"
                      "model_dtype 留空。"]

    if method == "bitsandbytes":
        if arch.get("quant_load_in_4bit"):
            return "int4", notes
        if arch.get("quant_load_in_8bit"):
            return "int8", notes
        return None, ["bitsandbytes 未声明 load_in_4bit / load_in_8bit，model_dtype 留空。"]

    # awq / gptq / moe_wna16 / auto-round：位宽在 bits / weight_bits
    got = _bits_to_model_dtype(arch.get("quant_bits"))
    if got:
        return got, notes
    return None, [f"{method} 未声明 bits / weight_bits，model_dtype 留空。"]


# ════════════════════════════════════════════════════════════════════════
# 5. 调度批量默认值（依赖显存 + 上下文长度）
# ════════════════════════════════════════════════════════════════════════
#
# vllm 侧按 `vllm serve` 的 UsageContext.OPENAI_API_SERVER 取值——这是我们
# 实际的压测形态（离线 LLM(...) 用的是另一套更大的默认值，此处不适用）。

_VLLM_LARGE_GPU_GIB = 70  # get_batch_defaults: device_memory >= 70 GiB 走大默认值


def _vllm_batch_defaults(gpu_mem_gib: float | None, gpu_type: str) -> tuple[int, int]:
    """
    照搬 vllm arg_utils.get_batch_defaults 的 OPENAI_API_SERVER 分支。

    显存 ≥70GiB 且卡名不含 a100 → (8192, 1024)，否则 (2048, 256)。
    a100 例外是上游针对 PR #17885 的性能回归特判，不是笔误。
    显存未知时按小档走（与上游取不到设备信息时 device_memory=0 的兜底一致）。
    """
    name = (gpu_type or "").lower()
    if gpu_mem_gib is not None and gpu_mem_gib >= _VLLM_LARGE_GPU_GIB and "a100" not in name:
        return 8192, 1024
    return 2048, 256


def derive_vllm_batch_params(
    gpu_mem_gib: float | None,
    gpu_type: str,
    max_model_len: int | None,
    *,
    explicit_batched_tokens: int | None = None,
    explicit_max_seqs: int | None = None,
    chunked_prefill_enabled: bool = True,
) -> tuple[int | None, int | None]:
    """
    vllm 的 (max_num_batched_tokens, max_num_seqs) 生效值
    → 映射到我们的 (chunked_prefill_size, max_running_requests)。

    完整复现 _set_default_max_num_seqs_and_batched_tokens_args 的顺序：
      1. 按显存取档位默认值；
      2. 未显式设置 batched_tokens 且未开 chunked prefill →
         抬到 max(max_model_len, 默认值)；
      3. 未显式设置 batched_tokens → 压到 min(max_num_seqs × max_model_len, 值)；
      4. 未显式设置 max_seqs → 压到 min(max_seqs, batched_tokens)。
    第 2/3 步都依赖 max_model_len；它为 None（无 config）时跳过这两步收敛，
    只给档位默认值。
    """
    default_tokens, default_seqs = _vllm_batch_defaults(gpu_mem_gib, gpu_type)
    tokens = explicit_batched_tokens if explicit_batched_tokens is not None else default_tokens
    seqs = explicit_max_seqs if explicit_max_seqs is not None else default_seqs

    if explicit_batched_tokens is None and max_model_len:
        if not chunked_prefill_enabled:
            tokens = max(max_model_len, tokens)
        tokens = min(seqs * max_model_len, tokens)
    if explicit_max_seqs is None:
        seqs = min(seqs, tokens)
    return tokens, seqs


def derive_sglang_chunked_prefill_size(gpu_mem_gib: float | None, tp_size: int) -> int | None:
    """
    sglang chunked_prefill_size 的显存档位默认值。

    直接调 gpu_memory_presets._gpu_mem_tier —— 那里已经把
    server_args.py:_handle_gpu_memory_settings 的分档搬过一遍，
    不在本模块再抄第二份（抄两份必然只改一边）。
    """
    if gpu_mem_gib is None:
        return None
    return gmp._gpu_mem_tier(gpu_mem_gib * 1024, max(int(tp_size or 1), 1))["chunked_prefill_size"]


def gpu_memory_gib(gpu_type: str | None) -> float | None:
    """显卡型号 → 单卡显存 GiB；型号表里没有则返回 None（不猜）。"""
    if not gpu_type:
        return None
    return gmp.GPU_MEMORY_GIB.get(gpu_type)


# ════════════════════════════════════════════════════════════════════════
# 6. 其余 DERIVED 参数
# ════════════════════════════════════════════════════════════════════════

# vllm config/cache.py:DEFAULT_BLOCK_SIZE / sglang overrides.py:_page_size_default
_PAGE_SIZE_DEFAULT = {"vllm": 16, "sglang": 1}

# 上游明确不支持 prefix caching 的架构类型（vllm is_prefix_caching_supported：
# encoder-decoder 与 attention-free 返回 False；生成式 decoder / hybrid 为 True）。
_NO_PREFIX_CACHING_ARCH_HINTS = ("encoderdecoder", "forconditionalgeneration_t5",
                                 "whisper", "bart", "t5")


def derive_prefix_caching(framework: str, arch: dict) -> bool:
    """
    默认是否启用前缀缓存（命令里没写 --disable-radix-cache /
    --no-enable-prefix-caching 时的生效值）。

    sglang 默认 True（radix cache 常开）；
    vllm 走 is_prefix_caching_supported：生成式 decoder / hybrid 为 True，
    encoder-decoder 系（whisper/bart/t5）为 False。
    """
    if framework == "sglang":
        return True
    model_type = str(arch.get("model_type") or "").lower()
    arch_name = str(arch.get("arch") or "").lower()
    for hint in _NO_PREFIX_CACHING_ARCH_HINTS:
        if hint in model_type or hint in arch_name:
            return False
    return True


def _static_default(framework: str, key: str):
    """param_map 里该参数的静态字面量默认值；非 STATIC 则返回 None。"""
    pair = pm.PARAM_BY_KEY.get(key)
    if pair is None:
        return None
    side = pair[framework]
    return side["default"] if side["default_kind"] == pm.STATIC else None


# 由 param_map 静态默认值直接回填的参数。
# tp/pp/dp 不在此列——它们由 gpu_count.fill_parallel_defaults 负责（要先有值才能算卡数）。
_STATIC_FILL_KEYS = (
    "dcp", "kv_cache_dtype", "trust_remote_code", "seed", "mem_fraction",
)


# ════════════════════════════════════════════════════════════════════════
# 7. 总入口：显式参数 + config + 显卡 → 生效参数
# ════════════════════════════════════════════════════════════════════════

def resolve(
    framework: str,
    params: dict,
    *,
    explicit_keys=None,
    cfg: dict | None = None,
    gpu_type: str | None = None,
) -> dict:
    """
    把「启动命令解析出的显式参数」补全为「实际生效参数」。

    framework 用 param_map 侧的名字（vllm / sglang）；vllm-ascend 请先归一。
    params 会被**就地补全**（只填 None / 缺失的键，显式值一律不动）。

    explicit_keys 是命令里真正写了的参数 key 集合。必须由调用方传入：
    params 里的 tp/pp/dp 在算卡数时已被回填成 1，光看"有没有值"分不出
    "命令写了 tp 1" 和 "命令没写 tp"。缺省时退化为按非空判断。

    返回：
      {
        "sources": {param_key: explicit|config|gpu|static},
        "notes":   [人类可读的推导说明与精度提示],
        "model_arch": {...} | None,     # cfg 为 None 时不产出
      }

    没给 cfg 时仍会回填「不依赖模型」的那部分（静态默认值、显存档位），
    依赖模型的（context_length / dtype / quantization）留空并在 notes 里说明。
    """
    if explicit_keys is None:
        explicit_keys = {k for k, v in params.items() if v is not None}
    sources: dict[str, str] = {k: SRC_EXPLICIT for k in explicit_keys}
    notes: list[str] = []
    arch = normalize(cfg) if cfg else None

    def put(key, value, source):
        """只填空位；已有显式值时不覆盖。"""
        if value is None or params.get(key) is not None:
            return
        params[key] = value
        sources[key] = source

    gpu_mem = gpu_memory_gib(gpu_type)
    if gpu_type and gpu_mem is None:
        notes.append(
            f"显卡型号 {gpu_type} 不在 gpu_memory_presets.GPU_MEMORY_GIB 表内，"
            "跳过按显存推导的参数（chunked_prefill_size / max_running_requests）。")

    # ── 静态字面量默认值（与命令显式写同值等价）──
    for key in _STATIC_FILL_KEYS:
        put(key, _static_default(framework, key), SRC_STATIC)
    put("page_size", _PAGE_SIZE_DEFAULT.get(framework), SRC_STATIC)

    # ── 依赖模型 config 的参数 ──
    context_length = params.get("context_length")
    if cfg is not None:
        derived_len, len_key = derive_context_length(framework, cfg)
        if context_length is None:
            put("context_length", derived_len, SRC_CONFIG)
            context_length = derived_len
            notes.append(
                f"context_length={derived_len} 由 config.{len_key} 推导"
                f"（{framework} 上游同逻辑）；命令未显式设置。")
        elif context_length > derived_len:
            notes.append(
                f"⚠ 命令里的 context_length={context_length} 超过 config 支持的 "
                f"{derived_len}（config.{len_key}）；{framework} 实际启动会报错或需环境变量放行。")

        resolved_dtype = derive_dtype(framework, cfg, params.get("dtype"))
        if params.get("dtype") in (None, "auto"):
            params["dtype"] = resolved_dtype
            sources["dtype"] = SRC_CONFIG

        quant = derive_quantization(cfg, params.get("quantization"))
        if params.get("quantization") is None and quant:
            put("quantization", quant, SRC_CONFIG)

        put("prefix_caching", derive_prefix_caching(framework, arch), SRC_CONFIG)
    else:
        notes.append(
            "未提供模型 config.json：context_length / dtype / quantization / "
            "prefix_caching 无法推导，相关列留空。上传 config.json 可补全。")

    # ── 依赖显存（+ 上下文长度）的调度参数 ──
    if framework == "vllm":
        tokens, seqs = derive_vllm_batch_params(
            gpu_mem, gpu_type or "", context_length,
            explicit_batched_tokens=params.get("chunked_prefill_size"),
            explicit_max_seqs=params.get("max_running_requests"),
        )
        put("chunked_prefill_size", tokens, SRC_GPU)
        put("max_running_requests", seqs, SRC_GPU)
        if gpu_mem is not None and context_length is None:
            notes.append(
                "无 config.json 时 max_num_batched_tokens 只取到显存档位默认值，"
                "未做 min(max_num_seqs × max_model_len, …) 收敛，可能偏大。")
    else:
        put("chunked_prefill_size",
            derive_sglang_chunked_prefill_size(gpu_mem, params.get("tp") or 1),
            SRC_GPU)
        notes.append(
            "sglang 的 mem_fraction_static / max_running_requests / attention_backend "
            "依赖运行时状态（是否 MLA、DP attention、moe_a2a_backend 等），"
            "不做推导，列留空。")

    # 兜底：有值但没记来源的（如算卡数时回填的 tp/pp/dp）一律算静态默认值
    for key, val in params.items():
        if val is not None and key not in sources:
            sources[key] = SRC_STATIC

    out = {"sources": sources, "notes": notes}
    if arch is not None:
        arch = dict(arch)
        kv_dtype = params.get("kv_cache_dtype")
        if kv_dtype in (None, "auto"):
            kv_dtype = params.get("dtype")
        arch["kv_bytes_per_token"] = kv_bytes_per_token(arch, dtype_bytes(kv_dtype))
        out["model_arch"] = arch
        # 元信息列（model_dtype / model_params_b / model_weight_gb）——不是启动参数，
        # 单独一块返回，由 ingest 层写进 metadata 顶层。
        meta, meta_notes = resolve_model_meta(cfg, arch, params.get("dtype"))
        out["model_meta"] = meta
        notes.extend(meta_notes)
    return out


def resolve_model_meta(
    cfg: dict,
    arch: dict | None = None,
    effective_dtype: str | None = None,
) -> tuple[dict, list[str]]:
    """
    config.json → test_runs 的模型元信息列。

    返回 ({model_dtype, model_params_b, model_weight_gb}, 告警列表)。
    每项取不到就是 None，调用方据此留空——这三列都不参与任何计算，
    只做分组对比，编一个值反而会让"同模型两次上传对不上"。

    传了 config 时这三项就是入库值（用户不再手填），所以 model_params_b 虽然
    是估算的，也按"够准就是真值"的标准要求：dense / MoE / MLA / 多模态各族的
    实测偏差见 §2b，超出可信范围的情形一律返回 None 而不是给个近似数。
    """
    arch = arch if arch is not None else normalize(cfg)
    model_dtype, notes = derive_model_dtype(cfg, arch, effective_dtype)

    parts = estimate_params(arch)
    params_b = round(parts["total"] / 1e9, 2) if parts else None
    if parts is None:
        notes.append("config 缺核心形状字段，不估算参数量。")

    weight_gib, weight_notes = estimate_weight_gib(arch, model_dtype)
    # estimate_weight_gib 内部会重跑 estimate_params，形状类告警会与上面重复
    for note in weight_notes:
        if note not in notes:
            notes.append(note)

    return {
        "model_dtype": model_dtype,
        "model_params_b": params_b,
        "model_weight_gb": weight_gib,
    }, notes


# 用户填的参数量与 config 估算值的相对偏差告警阈值。
#
# 定得松（20%）是因为手填时习惯写标称值："7B" 模型实际 7.62B（差 8%）、
# "30B" 实际 30.5B。要拦的是数量级错误——config 传错了别的模型，或把 MoE 的
# 激活参数量（Qwen3-30B-A3B 的 A3B）当成总参数量填进来，那些都差好几倍。
MODEL_PARAMS_DRIFT_THRESHOLD = 0.20

MODEL_META_KEYS = ("model_params_b", "model_weight_gb", "model_dtype")


def merge_model_meta(meta: dict, derived: dict | None) -> list[str]:
    """
    把推导出的模型元信息合并进 metadata 顶层（就地修改 meta），返回告警列表。

    三列的合并规则是同一条：**没填就用推导值，填了以填的为准并在不一致时告警。**
    传了 config 的正常路径下用户三个都不填，直接落推导值；手填只是覆盖通道，
    留给"我知道这个 checkpoint 的真实布局和 config 声明的不一样"的情况。

    不静默覆盖用户值，是因为 config 只描述形状、不描述磁盘上真实存了什么
    （被裁剪过的 checkpoint、混合量化、外挂 draft 权重都会让两者对不上）。

    比对方式按列分：dtype / weight 直接比值，params_b 比相对偏差
    （超 MODEL_PARAMS_DRIFT_THRESHOLD 才报），因为估算值本身有百分之几的误差，
    严格相等会天天误报。
    """
    notes: list[str] = []
    if not derived:
        return notes

    for key in MODEL_META_KEYS:
        guess = derived.get(key)
        given = meta.get(key)
        if guess is None:
            continue
        if given in (None, ""):
            meta[key] = guess
            continue
        if key == "model_params_b":
            drift = abs(float(given) - guess) / guess
            if drift > MODEL_PARAMS_DRIFT_THRESHOLD:
                notes.append(
                    f"⚠ 手填 model_params_b={given}B，config.json 估算为 {guess}B"
                    f"（相差 {drift * 100:.0f}%）；已按手填值入库。请确认 config.json 是"
                    "这个模型的，且填的不是 MoE 的激活参数量。")
        elif str(given) != str(guess):
            notes.append(
                f"⚠ 手填 {key}={given}，与 config.json 推导值 {guess} 不一致；"
                "已按手填值入库，请确认传的是同一个模型/checkpoint。")

    return notes
