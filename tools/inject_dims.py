#!/usr/bin/env python3
"""
把本轮压测维度注入结果 JSON 的 _autores_dims 块，供 to_csv.py 优先读取。

用法：
  python inject_dims.py RESULT.json --kind text \\
      --random-input-len 1024 --prefix-rate 0.5

  python inject_dims.py RESULT.json --kind vlm \\
      --random-input-len 1024 --image-count 1 --video-count 0 \\
      --image-resolution 720x1280
"""
from __future__ import annotations

import argparse
import json
import sys


def inject(path: str, dims: dict) -> None:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    existing = data.get("_autores_dims")
    if not isinstance(existing, dict):
        existing = {}
    existing.update({k: v for k, v in dims.items() if v is not None})
    data["_autores_dims"] = existing
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"[OK] _autores_dims 已注入 → {path}: {existing}")


def main() -> None:
    p = argparse.ArgumentParser(description="注入 _autores_dims 到 bench 结果 JSON")
    p.add_argument("json_file", help="bench 结果 JSON 路径")
    p.add_argument("--kind", choices=["text", "vlm"], required=True)
    p.add_argument("--random-input-len", type=int, default=None)
    p.add_argument("--prefix-rate", type=float, default=None)
    p.add_argument("--image-count", type=int, default=None)
    p.add_argument("--video-count", type=int, default=None)
    p.add_argument("--image-resolution", default=None)
    args = p.parse_args()

    dims: dict = {"kind": args.kind}
    if args.random_input_len is not None:
        dims["random_input_len"] = args.random_input_len
    if args.kind == "text":
        if args.prefix_rate is not None:
            dims["prefix_rate"] = args.prefix_rate
    else:
        if args.image_count is not None:
            dims["image_count"] = args.image_count
        if args.video_count is not None:
            dims["video_count"] = args.video_count
        if args.image_resolution is not None:
            dims["image_resolution"] = args.image_resolution

    try:
        inject(args.json_file, dims)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] inject_dims 失败: {e}", file=sys.stderr)
        sys.exit(0)  # 不阻断压测


if __name__ == "__main__":
    main()
