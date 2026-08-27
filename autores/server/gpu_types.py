"""
显卡型号注册表的服务层（D25）。

读写真相源是 tools/gpu_types.json（经 tools/gpu_memory_presets.py），
本模块只做：字段校验、库内引用检查、对外统一错误类型。

约束（刻意）：
  - 删除时若 test_runs / vlm_test_runs 已有引用 → 拒绝（历史 gpu_type 是裸字符串）
  - update 不允许改 name（改名等于让历史记录变孤儿；要改名只能新建）
"""
from __future__ import annotations

import importlib.util
import os
import re
import threading
from typing import Any

_GPU_PRESETS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools", "gpu_memory_presets.py",
)
_gmp = None
_gmp_lock = threading.Lock()

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MEMORY_MIN, _MEMORY_MAX = 1.0, 2048.0
_CARDS_MIN, _CARDS_MAX = 1, 64
_NOTE_MAX = 200


class GpuTypeError(Exception):
    """显卡型号操作非法；上层转成 400 / MCP ok:false。"""


def _load_gmp():
    global _gmp
    if _gmp is not None:
        return _gmp
    with _gmp_lock:
        if _gmp is not None:
            return _gmp
        spec = importlib.util.spec_from_file_location(
            "_autores_gpu_presets_svc", _GPU_PRESETS)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载显卡注册表模块: {_GPU_PRESETS}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _gmp = mod
    return _gmp


def vendor_presets() -> list[str]:
    """常见厂商快捷选项（页面下拉预设）。"""
    return list(_load_gmp().VENDOR_PRESETS)


def vendor_choices() -> list[str]:
    """兼容旧 API 名；返回 vendor_presets。"""
    return vendor_presets()


def used_vendors() -> list[str]:
    """注册表里已出现过的厂商标识（去重排序），供自定义输入参考。"""
    seen: set[str] = set()
    for entry in _load_gmp().all_types():
        v = (entry.get("vendor") or "").strip().lower()
        if v:
            seen.add(v)
    return sorted(seen)


def usage_count(db, name: str) -> int:
    """该型号在 text + vlm 两张表中的记录总数。"""
    if not name:
        return 0
    total = 0
    for kind in ("text", "vlm"):
        total += int(db.count_runs("gpu_type = ?", [name], kind=kind))
    return total


def _annotate(db, entry: dict) -> dict:
    out = dict(entry)
    out["in_use"] = usage_count(db, entry["name"]) if db is not None else 0
    out["total_memory_gib"] = (
        float(entry["memory_gib"]) * int(entry["cards_per_machine"])
    )
    return out


def list_types(db=None) -> list[dict]:
    """全部型号，每项附 in_use / total_memory_gib。"""
    return [_annotate(db, e) for e in _load_gmp().all_types()]


def get_type(db, name: str) -> dict | None:
    entry = _load_gmp().get_type(name)
    if entry is None:
        return None
    return _annotate(db, entry)


def _as_bool(val, label: str) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)) and val in (0, 1):
        return bool(val)
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
    raise GpuTypeError(f"{label} 必须是布尔值，收到: {val!r}")


def _validate_fields(
    *,
    name: str | None = None,
    memory_gib: Any = None,
    cards_per_machine: Any = None,
    vendor: Any = None,
    released: Any = None,
    note: Any = None,
    require_name: bool = False,
    require_memory: bool = False,
    require_vendor: bool = False,
) -> dict:
    """校验并归一化字段；只返回调用方显式传入的键。"""
    out: dict = {}

    if name is not None or require_name:
        if name is None or not str(name).strip():
            raise GpuTypeError("缺少必填字段: name")
        n = str(name).strip()
        if not _NAME_RE.match(n):
            raise GpuTypeError(
                f"型号名非法（需匹配 {_NAME_RE.pattern}），收到: {n!r}")
        out["name"] = n

    if memory_gib is not None or require_memory:
        if memory_gib is None:
            raise GpuTypeError("缺少必填字段: memory_gib")
        try:
            mem = float(memory_gib)
        except (TypeError, ValueError) as e:
            raise GpuTypeError(
                f"memory_gib 必须是数字，收到: {memory_gib!r}") from e
        if not (_MEMORY_MIN <= mem <= _MEMORY_MAX):
            raise GpuTypeError(
                f"memory_gib 须在 [{_MEMORY_MIN}, {_MEMORY_MAX}] GiB，收到: {mem}")
        out["memory_gib"] = mem

    if cards_per_machine is not None:
        try:
            cards = int(cards_per_machine)
        except (TypeError, ValueError) as e:
            raise GpuTypeError(
                f"cards_per_machine 必须是整数，收到: {cards_per_machine!r}"
            ) from e
        if not (_CARDS_MIN <= cards <= _CARDS_MAX):
            raise GpuTypeError(
                f"cards_per_machine 须在 [{_CARDS_MIN}, {_CARDS_MAX}]，收到: {cards}")
        out["cards_per_machine"] = cards

    if vendor is not None or require_vendor:
        if vendor is None or not str(vendor).strip():
            raise GpuTypeError("缺少必填字段: vendor")
        try:
            out["vendor"] = _load_gmp().normalize_vendor(vendor)
        except ValueError as e:
            raise GpuTypeError(str(e)) from e

    if released is not None:
        out["released"] = _as_bool(released, "released")

    if note is not None:
        n = str(note)
        if len(n) > _NOTE_MAX:
            raise GpuTypeError(f"note 最长 {_NOTE_MAX} 字，收到 {len(n)} 字")
        out["note"] = n

    return out


def create_type(
    db,
    *,
    name: str,
    memory_gib: float,
    cards_per_machine: int = 8,
    vendor: str = "",
    released: bool = True,
    note: str = "",
) -> dict:
    """新增型号；重名报错。"""
    fields = _validate_fields(
        name=name,
        memory_gib=memory_gib,
        cards_per_machine=cards_per_machine,
        vendor=vendor,
        released=released,
        note=note,
        require_name=True,
        require_memory=True,
        require_vendor=True,
    )
    gmp = _load_gmp()
    if gmp.get_type(fields["name"]) is not None:
        raise GpuTypeError(f"型号已存在: {fields['name']}")
    entry = {
        "name": fields["name"],
        "memory_gib": fields["memory_gib"],
        "cards_per_machine": fields.get("cards_per_machine", 8),
        "vendor": fields["vendor"],
        "released": fields.get("released", True),
        "note": fields.get("note", ""),
    }
    saved = gmp.upsert_type(entry)
    return _annotate(db, saved)


def update_type(
    db,
    name: str,
    *,
    memory_gib: Any = None,
    cards_per_machine: Any = None,
    vendor: Any = None,
    released: Any = None,
    note: Any = None,
) -> dict:
    """
    修改已有型号的非 name 字段。
    name 不可改——若调用方试图传入不同 name，直接拒绝。
    """
    gmp = _load_gmp()
    existing = gmp.get_type(name)
    if existing is None:
        raise GpuTypeError(f"型号不存在: {name}")

    patch = _validate_fields(
        memory_gib=memory_gib,
        cards_per_machine=cards_per_machine,
        vendor=vendor,
        released=released,
        note=note,
    )
    if not patch:
        raise GpuTypeError("未提供任何可修改字段")

    merged = dict(existing)
    merged.update(patch)
    # 显式锁死 name，防止误改
    merged["name"] = existing["name"]
    saved = gmp.upsert_type(merged)
    return _annotate(db, saved)


def delete_type(db, name: str) -> dict:
    """
    删除型号。若库内仍有引用则拒绝，返回 in_use 供上层展示。
    成功返回 {"ok": True, "deleted": name}。
    """
    gmp = _load_gmp()
    existing = gmp.get_type(name)
    if existing is None:
        raise GpuTypeError(f"型号不存在: {name}")
    used = usage_count(db, name)
    if used > 0:
        raise GpuTypeError(
            f"型号 {name} 仍被 {used} 条测试记录引用，无法删除。"
            "请先迁移或删除相关记录。"
        )
    if not gmp.delete_type(name):
        raise GpuTypeError(f"型号不存在: {name}")
    return {"ok": True, "deleted": name}


def preview_create(**kwargs) -> dict:
    """校验 create 入参并返回将写入的条目（不落盘）。"""
    fields = _validate_fields(
        name=kwargs.get("name"),
        memory_gib=kwargs.get("memory_gib"),
        cards_per_machine=kwargs.get("cards_per_machine", 8),
        vendor=kwargs.get("vendor"),
        released=kwargs.get("released", True),
        note=kwargs.get("note", ""),
        require_name=True,
        require_memory=True,
        require_vendor=True,
    )
    gmp = _load_gmp()
    if gmp.get_type(fields["name"]) is not None:
        raise GpuTypeError(f"型号已存在: {fields['name']}")
    entry = {
        "name": fields["name"],
        "memory_gib": fields["memory_gib"],
        "cards_per_machine": fields.get("cards_per_machine", 8),
        "vendor": fields["vendor"],
        "released": fields.get("released", True),
        "note": fields.get("note", ""),
    }
    entry["total_memory_gib"] = (
        float(entry["memory_gib"]) * int(entry["cards_per_machine"])
    )
    return entry


def preview_update(name: str, **kwargs) -> tuple[dict, dict]:
    """返回 (before, after) 预览；不落盘。"""
    gmp = _load_gmp()
    existing = gmp.get_type(name)
    if existing is None:
        raise GpuTypeError(f"型号不存在: {name}")
    patch = _validate_fields(
        memory_gib=kwargs.get("memory_gib"),
        cards_per_machine=kwargs.get("cards_per_machine"),
        vendor=kwargs.get("vendor"),
        released=kwargs.get("released"),
        note=kwargs.get("note"),
    )
    if not patch:
        raise GpuTypeError("未提供任何可修改字段")
    after = dict(existing)
    after.update(patch)
    after["name"] = existing["name"]
    return dict(existing), after


def preview_delete(db, name: str) -> dict:
    """删除预览：含 in_use；若有引用仍抛错（与真正删除一致）。"""
    gmp = _load_gmp()
    existing = gmp.get_type(name)
    if existing is None:
        raise GpuTypeError(f"型号不存在: {name}")
    used = usage_count(db, name)
    if used > 0:
        raise GpuTypeError(
            f"型号 {name} 仍被 {used} 条测试记录引用，无法删除。"
            "请先迁移或删除相关记录。"
        )
    return {"name": name, "in_use": used, "before": existing}
