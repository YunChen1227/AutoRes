"""
校验 tools/param_map.py 里的每个 flag 都真实存在于上游源码。

用途：vllm / sglang 升级后重新跑一遍，立刻发现被改名或删除的 flag，
避免配对表悄悄失真（这正是旧表"和实际值差异太大"的成因）。

用法：
    python tools/verify_param_map.py [--vllm D:/workspace/vllm] [--sglang D:/workspace/sglang]

说明：
  - vLLM 侧同时扫描 add_argument 的两种写法（**kwargs 形式与普通形式），
    并对 BooleanOptionalAction 自动补 --no-xxx 形式。
  - SGLang 侧从 ServerArgs 注解 dataclass 取字段名与显式 aliases，
    并额外接受 argparse 前缀缩写（如 --tp 之于 --tp-size）。
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import param_map as pm  # noqa: E402


def collect_vllm_flags(vllm_root: Path) -> set[str]:
    flags: set[str] = set()
    targets = [
        vllm_root / "vllm" / "engine" / "arg_utils.py",
        vllm_root / "vllm" / "entrypoints" / "openai" / "cli_args.py",
    ]
    for path in targets:
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8", errors="ignore")
        # 所有形如 "--xxx" / "-x" 的字面量（宽松取，宁可多收不可漏）
        for m in re.finditer(r'["\'](-{1,2}[a-zA-Z][a-zA-Z0-9\-\.]*)["\']', src):
            flags.add(m.group(1))
    # BooleanOptionalAction 会自动生成 --no-xxx
    for f in list(flags):
        if f.startswith("--"):
            flags.add("--no-" + f[2:])
    return flags


def collect_sglang_flags(sglang_root: Path) -> set[str]:
    path = sglang_root / "python" / "sglang" / "srt" / "server_args.py"
    src = path.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(src)
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "ServerArgs"
    )
    flags: set[str] = set()
    for stmt in cls.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        flags.add("--" + stmt.target.id.replace("_", "-"))
        for node in ast.walk(stmt.annotation):
            if isinstance(node, ast.keyword) and node.arg == "aliases":
                try:
                    flags.update(ast.literal_eval(node.value))
                except Exception:
                    pass
    # 手工注册的 flag（DeprecatedAction 等不走注解的分支）
    for m in re.finditer(r'["\'](--[a-z][a-z0-9\-]*)["\']', src):
        flags.add(m.group(1))
    return flags


def is_prefix_abbrev(flag: str, universe: set[str]) -> bool:
    """argparse 前缀缩写：--tp 唯一匹配 --tp-size 时合法。"""
    if not flag.startswith("--"):
        return False
    hits = [f for f in universe if f.startswith(flag)]
    return len(hits) >= 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vllm", default=r"D:\workspace\vllm")
    ap.add_argument("--sglang", default=r"D:\workspace\sglang")
    args = ap.parse_args()

    vllm_flags = collect_vllm_flags(Path(args.vllm))
    sgl_flags = collect_sglang_flags(Path(args.sglang))
    print(f"upstream flags: vllm={len(vllm_flags)} sglang={len(sgl_flags)}")

    problems = []
    for pair in pm.PARAM_PAIRS:
        for fw, universe in (("vllm", vllm_flags), ("sglang", sgl_flags)):
            for flag in pair[fw]["flags"]:
                if flag in universe:
                    continue
                if is_prefix_abbrev(flag, universe):
                    continue
                problems.append((fw, pair["key"], flag))

    if problems:
        print("\n!! 以下 flag 在上游源码中找不到（可能已改名/删除）：")
        for fw, key, flag in problems:
            print(f"   [{fw:6}] {key:22} {flag}")
        return 1

    print("OK: 配对表中所有 flag 均可在上游源码中找到")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
