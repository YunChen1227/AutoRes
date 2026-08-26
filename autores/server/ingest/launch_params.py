"""
复用 tools/to_csv.py 的启动参数提取规则（design.md §5.4）。

tools/ 是给测试人员直接跑的独立脚本目录（无 __init__.py，不是包），
但"从 launch_cmd 提取结构化参数"这套规则必须与落盘脚本**完全一致**——
否则同一条启动命令走目录流与走上传流会得到不同的 params，库里出现虚假差异。
故这里按文件路径加载该模块，而不是复制一份规则。
"""
from __future__ import annotations

import importlib.util
import os
import threading

_TOOLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "tools",
)
_TO_CSV = os.path.join(_TOOLS_DIR, "to_csv.py")
_PARAM_MAP_PD = os.path.join(_TOOLS_DIR, "param_map_pd.py")
_GPU_COUNT = os.path.join(_TOOLS_DIR, "gpu_count.py")

_module = None
_pd_module = None
_gc_module = None
_lock = threading.Lock()


def _load():
    """按路径加载 tools/to_csv.py（首次调用时加载一次，之后复用）。"""
    global _module
    if _module is not None:
        return _module
    with _lock:
        if _module is not None:
            return _module
        spec = importlib.util.spec_from_file_location("_autores_to_csv", _TO_CSV)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载启动参数提取规则: {_TO_CSV}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _module = mod
    return _module


def _load_pd():
    """按路径加载 tools/param_map_pd.py（PD 分离解析表）。"""
    global _pd_module
    if _pd_module is not None:
        return _pd_module
    with _lock:
        if _pd_module is not None:
            return _pd_module
        spec = importlib.util.spec_from_file_location("_autores_param_map_pd", _PARAM_MAP_PD)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 PD 分离解析表: {_PARAM_MAP_PD}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _pd_module = mod
    return _pd_module


def _load_gc():
    global _gc_module
    if _gc_module is not None:
        return _gc_module
    with _lock:
        if _gc_module is not None:
            return _gc_module
        spec = importlib.util.spec_from_file_location("_autores_gpu_count", _GPU_COUNT)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载卡数计算模块: {_GPU_COUNT}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _gc_module = mod
    return _gc_module


def supported_frameworks() -> list[str]:
    """支持的框架列表（与 tools/to_csv.py --framework choices 一致）。"""
    return ["sglang", "vllm", "vllm-ascend"]


def router_policies() -> list[str]:
    """router 可选路由策略（供前端下拉参考，仍接受自由输入）。"""
    return list(_load_pd().ROUTER_POLICIES)


def transfer_backends() -> dict[str, list[str]]:
    """各框架 KV 传输后端参考取值（sglang 是 flag 取值，vllm 是连接器名）。"""
    pd = _load_pd()
    return {"sglang": list(pd.SGL_TRANSFER_BACKENDS)}


def _model_config_mod():
    """
    模型 config 推导模块。

    取 to_csv 自己 import 的那一份（to_csv.py 顶部 `import model_config as mc`），
    而不是再按路径加载一次——同一份规则在进程里只能有一个实例，
    否则将来给它加缓存或状态时两边会不一致。
    """
    return _load().mc


def load_model_config(raw) -> dict:
    """模型 config.json（bytes/str/dict）→ dict。非法时抛 ValueError 子类。"""
    return _model_config_mod().load_config(raw)


def model_config_filename() -> str:
    """时间戳目录里存 config 原文的文件名（落盘与 Scanner 读回共用）。"""
    return _model_config_mod().RUN_DIR_FILENAME


def normalize_model_config(cfg: dict) -> dict:
    """模型 config dict → AutoRes model_arch 字段（层数/KV 头数/head_dim 等）。"""
    return _model_config_mod().normalize(cfg)


def model_meta(cfg: dict, arch: dict | None = None) -> tuple[dict, list[str]]:
    """
    模型 config dict → ({model_dtype, model_params_b, model_weight_gb}, 告警)。

    供 /api/upload/inspect-config 在用户选好文件时立刻回显预填值，
    不经过启动命令解析（那时前端还没提交命令）。
    """
    return _model_config_mod().resolve_model_meta(cfg, arch)


def merge_model_meta(meta: dict, derived: dict | None) -> list[str]:
    """把推导出的模型元信息合并进 metadata 顶层（就地修改），返回告警列表。"""
    return _model_config_mod().merge_model_meta(meta, derived)


def extract(framework: str, launch_cmd: str,
            model_cfg: dict | None = None,
            gpu_type: str | None = None) -> tuple[dict, dict]:
    """
    从启动命令 + 模型 config 提取 (params, extra)。framework 非法时抛 ValueError。

    model_cfg 为 None 时行为与只解析命令一致（依赖模型的参数留空）。
    """
    mod = _load()
    if framework not in supported_frameworks():
        raise ValueError(f"不支持的框架: {framework}")
    return mod.extract_launch_params(framework, launch_cmd, model_cfg, gpu_type)


def detect_role(framework: str, launch_cmd: str) -> str | None:
    """判断一条命令的 PD 角色：'prefill' / 'decode' / 'both' / None（非 PD）。"""
    return _load_pd().detect_role(framework, launch_cmd)


def looks_like_pd(launch_cmd: str) -> bool:
    """按关键字粗判是否 PD（disaggregation / kv-transfer-config），供自动跳转用。"""
    return _load_pd().looks_like_pd(launch_cmd)


def _pd_flag_names() -> set[str]:
    pd = _load_pd()
    return set(pd.SGL_PD_FLAGS) | {pd.VLLM_KV_FLAG}


def extract_role(framework: str, launch_cmd: str,
                 model_cfg: dict | None = None,
                 gpu_type: str | None = None) -> dict:
    """
    解析一条 PD 角色（prefill 或 decode）server 启动命令，返回：
      {
        role: 'prefill'|'decode'|'both',
        params: {...},        # 通用参数（tp/dp/...），由 param_map.py 解析
        disagg: {...},        # PD 专属字段（transfer_backend/kv_role/...）
        launch_cmd: str,
        unrecognized: [...],  # 未识别 flag（已剔除 PD 专属 flag）
        extra: {...},         # 通用参数解析的其它 extra（hicache_detail 等）
      }
    若该命令不是 PD 角色命令，role 为 None（调用方据此报错）。
    prefill 与 decode 跑同一个模型，调用方对两条命令传同一份 model_cfg。
    """
    pd = _load_pd()
    role = pd.detect_role(framework, launch_cmd)
    params, extra = extract(framework, launch_cmd, model_cfg, gpu_type)
    disagg = pd.extract_disagg(framework, launch_cmd)

    # 从 unrecognized 中剔除 PD 专属 flag（它们已被 disagg 解析，不算"未识别"）
    pd_flags = _pd_flag_names()
    unrecognized = []
    for item in extra.pop("unrecognized", []):
        first = item.split(" ", 1)[0]
        if first in pd_flags:
            continue
        unrecognized.append(item)

    gc = _load_gc()
    pm_fw = "vllm" if framework == "vllm-ascend" else framework
    gpu_count = gc.annotate_gpu_count(pm_fw, params, extra)

    return {
        "role": role,
        "params": params,
        "disagg": disagg,
        "gpu_count": gpu_count,
        "launch_cmd": launch_cmd,
        "unrecognized": unrecognized,
        "extra": extra,
    }


def parse_router(router_cmd: str | None) -> dict:
    """解析 router/proxy 启动命令，返回 {policy, prefill_policy, decode_policy, _extra}。"""
    return _load_pd().parse_router(router_cmd or "")


def pd_gpu_counts(framework: str, prefill_params: dict, decode_params: dict) -> tuple[int, int, int]:
    """PD 分离：回填并行度默认值后算 prefill/decode/总卡数。"""
    pm_fw = "vllm" if framework == "vllm-ascend" else framework
    return _load_gc().annotate_pd_gpu_counts(pm_fw, prefill_params, decode_params)
