#!/usr/bin/env python3
"""
AutoRes 性能饱和点（hardware wall）CLI。

计算逻辑在 autores.server.analysis.saturation（与 chatbot / MCP 同一实现）。
本脚本仅负责：定位数据库、只读取数、命令行参数与输出格式。

用法（在 AutoRes 仓库根目录）：
  python .cursor/skills/perf-saturation-analysis/scripts/analyze_wall.py --list
  python .cursor/skills/perf-saturation-analysis/scripts/analyze_wall.py \\
    --filter gpu_type=H20-141G --format md
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any

# 仓库根 → 可 import autores
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from autores.db import schema  # noqa: E402
from autores.server.analysis.saturation import (  # noqa: E402
    analyze_run,
    render_markdown,
    to_jsonable,
)

# CLI 允许的 --filter 键（元信息直接列；服务侧支持全部 ALL_DIMENSIONS）
META_FILTER_COLS = {
    "run_id", "model", "model_version", "framework", "framework_version",
    "gpu_type", "deployment_mode", "bench_framework", "bench_flush_cache",
    "prefix_rate",
}


def default_db_path() -> str:
    """相对本脚本定位仓库根，读 config.yaml 的 database.path。"""
    cfg_path = os.path.join(_REPO, "config.yaml")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(_REPO, "config.example.yaml")
    db_rel = "var/data/autores.db"
    if os.path.exists(cfg_path):
        try:
            import yaml  # optional
            with open(cfg_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            db_rel = (raw.get("database") or {}).get("path") or db_rel
        except Exception:
            with open(cfg_path, "r", encoding="utf-8") as f:
                in_db = False
                for line in f:
                    s = line.strip()
                    if s.startswith("database:"):
                        in_db = True
                        continue
                    if in_db:
                        if s.startswith("path:"):
                            db_rel = s.split(":", 1)[1].strip().strip("\"'")
                            break
                        if s and not s.startswith("#") and not s.startswith("path"):
                            break
    if os.path.isabs(db_rel):
        return db_rel
    return os.path.normpath(os.path.join(_REPO, db_rel))


def connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SystemExit(f"[ERR] database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_filters(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"[ERR] --filter must be key=value, got: {p}")
        k, v = p.split("=", 1)
        k, v = k.strip(), v.strip()
        if k not in META_FILTER_COLS:
            raise SystemExit(
                f"[ERR] unknown filter key '{k}'. allowed: {sorted(META_FILTER_COLS)}")
        out[k] = v
    return out


def fetch_runs(
    conn: sqlite3.Connection,
    filters: dict[str, str],
    run_id: str | None,
) -> list[dict]:
    """只读取数，经 schema.row_to_doc 转为服务侧文档形态。"""
    sql = "SELECT * FROM test_runs"
    clauses: list[str] = []
    params: list[Any] = []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    for k, v in filters.items():
        if k == "run_id":
            clauses.append("run_id = ?")
            params.append(v)
            continue
        if k == "bench_flush_cache":
            if v.lower() in ("true", "1", "yes"):
                clauses.append(f"({k} = 1 OR {k} = 'true')")
            elif v.lower() in ("false", "0", "no"):
                clauses.append(f"({k} = 0 OR {k} = 'false' OR {k} IS NULL)")
            else:
                clauses.append(f"{k} = ?")
                params.append(v)
        else:
            clauses.append(f"{k} = ?")
            params.append(v)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY run_timestamp"
    rows = conn.execute(sql, params).fetchall()
    return [schema.row_to_doc(r) for r in rows]


def list_runs_md(docs: list[dict]) -> str:
    lines = [
        "# Matching runs", "",
        "| run_id | model | gpu | framework | tp/dp | metrics |",
        "|---|---|---|---|---|---:|",
    ]
    for d in docs:
        params = d.get("params") or {}
        rid = d.get("_id") or d.get("run_id")
        lines.append(
            f"| `{rid}` | {d.get('model')} | {d.get('gpu_type')} | "
            f"{d.get('framework')} | {params.get('tp')}/{params.get('dp')} | "
            f"{len(d.get('metrics') or [])} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AutoRes hardware-wall / saturation analyzer")
    p.add_argument("--db", default="", help="SQLite path (default: config.yaml database.path)")
    p.add_argument("--filter", action="append", default=[], help="key=value, repeatable")
    p.add_argument("--run-id", default="", help="exact run_id")
    p.add_argument("--list", action="store_true", help="list matching runs only")
    p.add_argument("--plateau-gain", type=float, default=0.10)
    p.add_argument("--latency-factor", type=float, default=2.0)
    p.add_argument("--headroom", type=float, default=0.8)
    p.add_argument("--retro-tol", type=float, default=0.05,
                   help="fractional drop below peak to mark retrograde")
    p.add_argument("--slo-ttft-p99", type=float, default=None)
    p.add_argument("--slo-tpot-mean", type=float, default=None)
    p.add_argument("--slo-itl-p95", type=float, default=None)
    p.add_argument("--slo-e2e-p99", type=float, default=None)
    p.add_argument("--format", choices=["md", "json"], default="md")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    db = args.db or default_db_path()
    filters = parse_filters(args.filter)
    run_id = args.run_id or None

    conn = connect(db)
    try:
        docs = fetch_runs(conn, filters, run_id)
    finally:
        conn.close()

    if not docs:
        msg = f"No runs matched (db={db}, filters={filters}, run_id={run_id})"
        if args.format == "json":
            print(json.dumps({"error": msg}, ensure_ascii=False, indent=2))
        else:
            print(f"[ERR] {msg}", file=sys.stderr)
        return 1

    if args.list:
        if args.format == "json":
            print(json.dumps([
                {
                    "run_id": d.get("_id"),
                    "model": d.get("model"),
                    "gpu_type": d.get("gpu_type"),
                    "framework": d.get("framework"),
                    "n_metrics": len(d.get("metrics") or []),
                }
                for d in docs
            ], ensure_ascii=False, indent=2))
        else:
            print(list_runs_md(docs))
        return 0

    slo = {
        "ttft_p99": args.slo_ttft_p99,
        "tpot_mean": args.slo_tpot_mean,
        "itl_p95": args.slo_itl_p95,
        "e2e_p99": args.slo_e2e_p99,
    }
    kwargs = dict(
        plateau_gain=args.plateau_gain,
        latency_factor=args.latency_factor,
        headroom=args.headroom,
        retro_tol=args.retro_tol,
        slo=slo,
    )
    results = [analyze_run(d, **kwargs) for d in docs]

    if args.format == "json":
        print(json.dumps(to_jsonable(results, include_points=True),
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(render_markdown(results, slo, include_points=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
