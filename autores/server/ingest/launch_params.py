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

_TO_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "tools", "to_csv.py",
)

_module = None
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


def supported_frameworks() -> list[str]:
    """支持的框架列表（= 默认值表的键，前端下拉与后端校验共用同一来源）。"""
    return sorted(_load().FRAMEWORK_DEFAULTS.keys())


def extract(framework: str, launch_cmd: str) -> tuple[dict, dict]:
    """从启动命令提取 (params, extra)。framework 非法时抛 ValueError。"""
    mod = _load()
    if framework not in mod.FRAMEWORK_DEFAULTS:
        raise ValueError(f"不支持的框架: {framework}")
    return mod.extract_launch_params(framework, launch_cmd)
