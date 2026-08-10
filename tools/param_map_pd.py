"""
PD 分离部署（Prefill-Decode Disaggregation）启动参数解析表。

与 param_map.py 同源、同风格，但只覆盖"单机/分布式"里不存在、PD 才有的那部分参数：
  - SGLang: python/sglang/srt/server_args.py 的 disaggregation 参数组
            （--disaggregation-mode / --disaggregation-transfer-backend / ...）
  - vLLM  : vllm/config/kv_transfer.py 的 KVTransferConfig
            （全部塞在一个 --kv-transfer-config '<JSON>' 里）
  - Router: sglang_router.launch_router / 自建 proxy 的路由策略
            （--policy / --prefill-policy / --decode-policy）

抽取基线：
  SGLang server_args.py（disaggregation 组，见下方各条 flag 注释的 default）
  vLLM   KVTransferConfig（kv_connector / kv_role / kv_rank / kv_parallel_size / ...）

────────────────────────────────────────────────────────────────────────
设计说明
────────────────────────────────────────────────────────────────────────
1. prefill / decode 两个实例，各自是一条**完整的 server 启动命令**，其 tp/dp/
   mem_fraction 等"通用参数"仍由 param_map.py 那张表解析（一条命令 = 一套通用参数）。
   本表只负责识别：
     a) 这条命令是不是 PD 角色命令、是 prefill 还是 decode（detect_role）；
     b) PD 专属字段（传输后端、bootstrap 端口、KV 角色/rank 等）。
2. 两边把"PD 角色"编码的方式完全不同：
     SGLang: --disaggregation-mode {prefill|decode}
     vLLM  : --kv-transfer-config 里的 kv_role {kv_producer|kv_consumer|kv_both}
             （kv_producer≈prefill、kv_consumer≈decode、kv_both≈两者合一）
3. "传输后端"两边取值空间不同但概念一致（KV 怎么在两实例间传）：
     SGLang: transfer_backend ∈ {mooncake(默认), nixl, ascend, fake, mori}
     vLLM  : kv_connector    ∈ {NixlConnector, LMCacheConnectorV1, P2pNcclConnector,
                                MultiConnector, SharedStorageConnector, MoRIIOConnector, ...}
   统一归到 pd_transfer_backend 一个字段（存原始取值，不强行翻译）。
4. 只对**真正静态**的默认值做回填（STATIC）；运行时推导/无固定值的不回填，
   与 param_map.py 的取舍一致。
"""
from __future__ import annotations

import json
import shlex

# ── 默认值种类（与 param_map.py 对齐）─────────────────────────────────────
STATIC = "static"
DERIVED = "derived"
NA = "na"


# ════════════════════════════════════════════════════════════════════════
# SGLang：disaggregation 参数组（server_args.py）
#   flag → (字段名, 类型, 静态默认值, 说明)
#   类型: int / float / str / bool
# ════════════════════════════════════════════════════════════════════════
SGL_DISAGG_MODE_FLAG = "--disaggregation-mode"
SGL_DISAGG_MODES = ["null", "prefill", "decode"]           # server_args 默认 "null"
SGL_TRANSFER_BACKENDS = ["mooncake", "nixl", "ascend", "fake", "mori"]

SGL_PD_FLAGS = {
    # 角色判定字段（值即 prefill/decode），单独由 detect_role 处理，这里也登记方便去重
    "--disaggregation-mode": ("disaggregation_mode", "str", None,
        "PD 角色：prefill / decode；默认 null 表示非 PD（server_args 默认 'null'）。"),
    "--disaggregation-transfer-backend": ("transfer_backend", "str", "mooncake",
        "KV 传输后端，默认 mooncake（server_args 默认）。"),
    "--disaggregation-bootstrap-port": ("bootstrap_port", "int", 8998,
        "prefill 侧 bootstrap server 端口，默认 8998。"),
    "--disaggregation-ib-device": ("ib_device", "str", None,
        "InfiniBand 设备；默认 None，mooncake 后端下自动探测。"),
    "--disaggregation-decode-enable-radix-cache": ("decode_radix_cache", "bool", False,
        "decode 侧 radix cache（PD 模式），默认关。"),
    "--disaggregation-decode-enable-offload-kvcache": ("decode_offload_kvcache", "bool", False,
        "decode 侧异步 KV 卸载（PD 模式），默认关。"),
    "--num-reserved-decode-tokens": ("num_reserved_decode_tokens", "int", 512,
        "新请求入 running batch 时为 decode 预留的 token 数，默认 512。"),
    "--disaggregation-decode-polling-interval": ("decode_polling_interval", "int", 1,
        "decode server 轮询请求的间隔，默认 1。"),
}

# ════════════════════════════════════════════════════════════════════════
# vLLM：KVTransferConfig（--kv-transfer-config '<JSON>'）
#   JSON key → (统一字段名, 说明, 静态默认值)
# ════════════════════════════════════════════════════════════════════════
VLLM_KV_FLAG = "--kv-transfer-config"
VLLM_KV_ROLES = ["kv_producer", "kv_consumer", "kv_both"]
VLLM_KV_ROLE_TO_PD = {"kv_producer": "prefill", "kv_consumer": "decode", "kv_both": "both"}

VLLM_KV_FIELDS = {
    "kv_connector":      ("transfer_backend", "KV 连接器/传输后端名（如 NixlConnector）。", None),
    "kv_role":           ("kv_role", "producer/consumer/both；映射到 prefill/decode。", None),
    "kv_rank":           ("kv_rank", "本实例在 KV 传输中的 rank（0=prefill、1=decode）。", None),
    "kv_parallel_size":  ("kv_parallel_size", "KV 传输并行实例数，默认 1。", 1),
    "kv_ip":             ("kv_ip", "KV 连接 IP，默认 127.0.0.1。", "127.0.0.1"),
    "kv_port":           ("kv_port", "KV 连接端口，默认 14579。", 14579),
    "kv_buffer_size":    ("kv_buffer_size", "KV 缓冲区字节数，默认 1e9。", 1e9),
    "kv_buffer_device":  ("kv_buffer_device", "KV 缓冲设备（cuda/cpu/xpu）。", None),
}

# ════════════════════════════════════════════════════════════════════════
# Router / Proxy：路由策略（sglang_router.launch_router 等）
#   flag → (字段名, 类型, 说明)
# ════════════════════════════════════════════════════════════════════════
ROUTER_POLICIES = ["cache_aware", "round_robin", "random", "power_of_two"]

ROUTER_FLAGS = {
    "--policy": ("policy", "str", "全局路由策略。"),
    "--prefill-policy": ("prefill_policy", "str", "prefill 侧路由策略。"),
    "--decode-policy": ("decode_policy", "str", "decode 侧路由策略。"),
}
# router 里对性能对比意义不大、但值得留档的连接/超时项 → 存 extra，不提列
ROUTER_EXTRA_FLAGS = {
    "--host": "host",
    "--port": "port",
    "--worker-startup-timeout-secs": "worker_startup_timeout_secs",
    "--prefill": "prefill_urls",       # 可多次出现
    "--decode": "decode_urls",         # 可多次出现
}


# ════════════════════════════════════════════════════════════════════════
# 解析工具（stdlib only，供 to_csv / launch_params / scanner 复用）
# ════════════════════════════════════════════════════════════════════════
def _split(cmd: str) -> list[str]:
    if not cmd:
        return []
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _iter_flags(tokens):
    """产出 (flag_name, value)；value 为 True 表示裸开关。与 to_csv._iter_flag_tokens 同规则。"""
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if not tok.startswith("-") or _is_negative_number(tok):
            i += 1
            continue
        name, sep, inline = tok.partition("=")
        if sep:
            yield name, inline
            i += 1
            continue
        nxt = tokens[i + 1] if i + 1 < n else None
        if nxt is not None and (not nxt.startswith("-") or _is_negative_number(nxt)):
            yield name, nxt
            i += 2
        else:
            yield name, True
            i += 1


def _is_negative_number(tok: str) -> bool:
    if not tok.startswith("-") or len(tok) < 2:
        return False
    return tok[1].isdigit() or (tok[1] == "." and len(tok) > 2 and tok[2].isdigit())


def _coerce(kind: str, raw):
    if raw is True:
        return True
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if kind == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    if kind == "bool":
        if isinstance(raw, str) and raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        return bool(raw) if raw is not None else raw
    return raw


def _vllm_kv_config(cmd: str) -> dict | None:
    """取出 --kv-transfer-config 的 JSON 值并解析；无该 flag 或解析失败返回 None。"""
    for name, value in _iter_flags(_split(cmd)):
        if name == VLLM_KV_FLAG and value is not True:
            try:
                obj = json.loads(value)
                return obj if isinstance(obj, dict) else None
            except (ValueError, TypeError):
                return None
    return None


def detect_role(framework: str, cmd: str) -> str | None:
    """
    判断一条 server 启动命令的 PD 角色。

    返回 'prefill' / 'decode' / 'both'，若不是 PD 角色命令则返回 None。
      SGLang: 看 --disaggregation-mode 的取值（null/缺省 → 非 PD）
      vLLM  : 看 --kv-transfer-config 里的 kv_role
    """
    if not cmd:
        return None
    if framework == "sglang":
        for name, value in _iter_flags(_split(cmd)):
            if name == SGL_DISAGG_MODE_FLAG and value is not True:
                v = str(value).strip().lower()
                return v if v in ("prefill", "decode") else None
        return None
    if framework in ("vllm", "vllm-ascend"):
        cfg = _vllm_kv_config(cmd)
        if not cfg:
            return None
        role = str(cfg.get("kv_role") or "").strip().lower()
        return VLLM_KV_ROLE_TO_PD.get(role)
    return None


def is_pd_command(framework: str, cmd: str) -> bool:
    """这条命令是否为 PD 角色命令。"""
    return detect_role(framework, cmd) is not None


def looks_like_pd(cmd: str) -> bool:
    """
    仅按关键字粗判（不依赖框架），供前端/上传"用户填了字段就自动跳转 PD"用。
    命中 disaggregation 相关字段或 vLLM 的 kv-transfer-config 即认为是 PD。
    """
    if not cmd:
        return False
    low = cmd.lower()
    return ("disaggregation" in low) or ("kv-transfer-config" in low) or ("kv_role" in low)


def extract_disagg(framework: str, cmd: str, *, fill_defaults: bool = True) -> dict:
    """
    从一条 PD 角色命令里提取 PD 专属字段（不含 tp/dp 等通用参数，那些走 param_map.py）。

    返回 dict，可能包含：
      role                  prefill / decode / both（由 detect_role 判定）
      transfer_backend      KV 传输后端（sglang transfer_backend / vllm kv_connector）
      bootstrap_port / ib_device / decode_offload_kvcache / ...（sglang）
      kv_role / kv_rank / kv_parallel_size / kv_ip / kv_port / ...（vllm）
    fill_defaults=True 时对**静态**默认值做回填（如 sglang transfer_backend=mooncake）。
    """
    out: dict = {}
    role = detect_role(framework, cmd)
    if role is None:
        return out
    out["role"] = role

    if framework == "sglang":
        seen = {}
        for name, value in _iter_flags(_split(cmd)):
            spec = SGL_PD_FLAGS.get(name)
            if spec is None:
                continue
            field, kind, _default, _note = spec
            if field == "disaggregation_mode":
                continue  # 已由 role 表达
            seen[field] = _coerce(kind, value)
        out.update(seen)
        if fill_defaults:
            for _flag, (field, _kind, default, _note) in SGL_PD_FLAGS.items():
                if field in ("disaggregation_mode",):
                    continue
                if default is not None and field not in out:
                    out[field] = default

    elif framework in ("vllm", "vllm-ascend"):
        cfg = _vllm_kv_config(cmd) or {}
        for jkey, (field, _note, default) in VLLM_KV_FIELDS.items():
            if jkey in cfg:
                out[field] = cfg[jkey]
            elif fill_defaults and default is not None:
                out[field] = default
        # 额外连接器配置原样留档
        extra_cfg = cfg.get("kv_connector_extra_config")
        if extra_cfg:
            out["kv_connector_extra_config"] = extra_cfg

    return out


def parse_router(cmd: str) -> dict:
    """
    解析 router/proxy 启动命令。返回 dict：
      policy / prefill_policy / decode_policy   → 提列比较
      _extra                                    → host/port/urls/timeout 等留档
    命令为空返回 {}。
    """
    if not cmd or not cmd.strip():
        return {}
    out: dict = {}
    extra: dict = {}
    tokens = _split(cmd)
    for name, value in _iter_flags(tokens):
        if name in ROUTER_FLAGS:
            field, kind, _note = ROUTER_FLAGS[name]
            out[field] = _coerce(kind, value)
            continue
        exkey = ROUTER_EXTRA_FLAGS.get(name)
        if exkey is None:
            continue
        if exkey in ("prefill_urls", "decode_urls"):
            extra.setdefault(exkey, []).append(value)
        else:
            extra[exkey] = value
    if extra:
        out["_extra"] = extra
    return out


def transfer_backend_of(pd_fields: dict) -> str | None:
    """从 extract_disagg 结果里取统一的传输后端值。"""
    return pd_fields.get("transfer_backend")
