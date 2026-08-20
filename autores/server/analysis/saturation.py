"""
性能饱和点（hardware wall）分析。

从 test_runs.metrics 按 input_length 分组，检测吞吐平台 / 延迟膝点 /
简化 Kneedle / USL N* / SLO 上限；供 chatbot、MCP、CLI 共用。

指标键与入库规范一致：维度小写 input_length/concurrency，其余为
CANONICAL_COLUMNS（Output_Throughput、ITL_P95(ms) 等）。
文档形态与 schema.row_to_doc 一致（_id、嵌套 params）。
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from autores.db import schema
from autores.server.report.query import build_conditions

# ── 指标列名（容忍缺列）──────────────────────────────────────────────

TP_KEYS = ("Output_Throughput", "Total_Throughput", "Request_Throughput")
ITL_KEYS = ("ITL_P95(ms)", "ITL_P99(ms)", "ITL_Mean(ms)", "TPOT_Mean(ms)")
TTFT_KEYS = ("TTFT_P95(ms)", "TTFT_P99(ms)", "TTFT_Mean(ms)")
TPOT_KEYS = ("TPOT_P95(ms)", "TPOT_P99(ms)", "TPOT_Mean(ms)")
E2E_KEYS = ("E2E_P99(ms)", "E2E_Mean(ms)")

DEFAULT_MAX_RUNS = 5


# ══════════════════════════════════════════════════════════════════════
# 数学工具
# ══════════════════════════════════════════════════════════════════════

def _num(v: Any) -> float | None:
    if v is None or v == "" or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first_num(row: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        n = _num(row.get(k))
        if n is not None:
            return n
    return None


def _solve_3x3(A: list[list[float]], b: list[float]) -> list[float] | None:
    """高斯消元解 3×3；奇异返回 None。"""
    M = [A[i][:] + [b[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-12:
            return None
        M[col], M[pivot] = M[pivot], M[col]
        div = M[col][col]
        M[col] = [x / div for x in M[col]]
        for row in range(3):
            if row == col:
                continue
            factor = M[row][col]
            M[row] = [M[row][c] - factor * M[col][c] for c in range(4)]
    return [M[i][3] for i in range(3)]


def fit_usl(points: list[tuple[float, float]]) -> dict:
    """
    USL: X(N) = γ N / (1 + α(N-1) + β N(N-1))
    线性化 y=N/X，基函数 [1, N-1, N(N-1)]，OLS 解 1/γ, α/γ, β/γ。
    需要 ≥4 个 (N, X>0) 点。
    """
    pts = [(n, x) for n, x in points if n > 0 and x is not None and x > 0]
    if len(pts) < 4:
        return {"ok": False, "reason": f"need >=4 points, got {len(pts)}"}

    ata = [[0.0] * 3 for _ in range(3)]
    atb = [0.0] * 3
    for n, x in pts:
        y = n / x
        basis = [1.0, n - 1.0, n * (n - 1.0)]
        for i in range(3):
            atb[i] += basis[i] * y
            for j in range(3):
                ata[i][j] += basis[i] * basis[j]

    coef = _solve_3x3(ata, atb)
    if coef is None:
        return {"ok": False, "reason": "singular normal matrix"}

    inv_g, a_over_g, b_over_g = coef
    if abs(inv_g) < 1e-15:
        return {"ok": False, "reason": "gamma near zero"}
    gamma = 1.0 / inv_g
    alpha = a_over_g * gamma
    beta = b_over_g * gamma

    result: dict[str, Any] = {
        "ok": True,
        "gamma": gamma,
        "alpha": alpha,
        "beta": beta,
        "n_star": None,
        "x_max": None,
    }
    if beta > 1e-15 and (1.0 - alpha) > 0:
        n_star = math.sqrt((1.0 - alpha) / beta)
        result["n_star"] = n_star
        denom = 1.0 + alpha * (n_star - 1.0) + beta * n_star * (n_star - 1.0)
        if denom > 0:
            result["x_max"] = gamma * n_star / denom
    return result


def simplified_kneedle(xs: list[float], ys: list[float]) -> int | None:
    """返回膝点索引：归一化后到首末连线垂直距离最大。"""
    if len(xs) < 3:
        return None
    x0, x1 = xs[0], xs[-1]
    y0, y1 = ys[0], ys[-1]
    dx = x1 - x0
    dy = y1 - y0
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return None
    xn = [(x - x0) / dx if abs(dx) > 1e-12 else 0.0 for x in xs]
    yn = [(y - y0) / dy if abs(dy) > 1e-12 else 0.0 for y in ys]
    best_i, best_d = 1, -1.0
    for i in range(1, len(xs) - 1):
        d = abs(yn[i] - xn[i])
        if d > best_d:
            best_d = d
            best_i = i
    return best_i


# ══════════════════════════════════════════════════════════════════════
# 分组与检测器
# ══════════════════════════════════════════════════════════════════════

def group_by_input_length(metrics: list[dict]) -> dict[float, list[dict]]:
    groups: dict[float, list[dict]] = defaultdict(list)
    for m in metrics:
        il = _num(m.get("input_length"))
        cc = _num(m.get("concurrency"))
        if il is None or cc is None:
            continue
        if il <= 0:
            continue
        groups[il].append(m)
    for il in groups:
        groups[il].sort(key=lambda r: _num(r.get("concurrency")) or 0)
    return dict(sorted(groups.items(), key=lambda kv: kv[0]))


@dataclass
class PointTrace:
    concurrency: float
    throughput: float | None
    gain: float | None
    itl: float | None
    itl_ratio: float | None
    ttft: float | None
    ttft_ratio: float | None
    tpot: float | None
    e2e: float | None
    completed: float | None
    littles_L: float | None  # Request_TP * E2E_s


@dataclass
class LengthAnalysis:
    input_length: float
    n_points: int
    points: list[PointTrace]
    plateau_c: float | None = None
    peak_c: float | None = None
    peak_tp: float | None = None
    retrograde_c: float | None = None
    kneedle_c: float | None = None
    itl_knee_c: float | None = None
    ttft_knee_c: float | None = None
    slo_max_c: float | None = None
    usl: dict | None = None
    wall_c: float | None = None
    recommended_c: float | None = None
    bottleneck: str = "inconclusive"
    confidence: str = "low"
    notes: list[str] = field(default_factory=list)


def analyze_length(
    input_length: float,
    rows: list[dict],
    *,
    plateau_gain: float,
    latency_factor: float,
    headroom: float,
    retro_tol: float,
    slo: dict[str, float | None],
) -> LengthAnalysis:
    traces: list[PointTrace] = []
    base_itl = None
    base_ttft = None

    for i, r in enumerate(rows):
        c = _num(r.get("concurrency")) or 0
        tp = _first_num(r, TP_KEYS)
        itl = _first_num(r, ITL_KEYS)
        ttft = _first_num(r, TTFT_KEYS)
        tpot = _first_num(r, TPOT_KEYS)
        e2e = _first_num(r, E2E_KEYS)
        completed = _num(r.get("Completed"))
        req_tp = _num(r.get("Request_Throughput"))
        littles = None
        if req_tp is not None and e2e is not None and e2e > 0:
            littles = req_tp * (e2e / 1000.0)

        gain = None
        if i > 0 and tp is not None and traces[-1].throughput not in (None, 0):
            prev = traces[-1].throughput
            gain = (tp - prev) / prev  # type: ignore[operator]

        if i == 0:
            base_itl = itl
            base_ttft = ttft
        itl_ratio = (itl / base_itl) if (itl is not None and base_itl and base_itl > 0) else None
        ttft_ratio = (ttft / base_ttft) if (ttft is not None and base_ttft and base_ttft > 0) else None

        traces.append(PointTrace(
            concurrency=c, throughput=tp, gain=gain,
            itl=itl, itl_ratio=itl_ratio,
            ttft=ttft, ttft_ratio=ttft_ratio,
            tpot=tpot, e2e=e2e, completed=completed, littles_L=littles,
        ))

    out = LengthAnalysis(input_length=input_length, n_points=len(traces), points=traces)

    for t in traces[1:]:
        if t.gain is not None and t.gain < plateau_gain:
            out.plateau_c = t.concurrency
            break

    valid_tp = [(t.concurrency, t.throughput) for t in traces if t.throughput is not None]
    if valid_tp:
        peak_c, peak_tp = max(valid_tp, key=lambda p: p[1])  # type: ignore[arg-type]
        out.peak_c = peak_c
        out.peak_tp = peak_tp
        after = False
        for c, tp in valid_tp:
            if c == peak_c:
                after = True
                continue
            if after and peak_tp and tp is not None and tp < peak_tp * (1.0 - retro_tol):
                out.retrograde_c = c
                break

    if len(valid_tp) >= 3:
        xs = [p[0] for p in valid_tp]
        ys = [p[1] for p in valid_tp]  # type: ignore[misc]
        idx = simplified_kneedle(xs, ys)  # type: ignore[arg-type]
        if idx is not None:
            out.kneedle_c = xs[idx]

    for t in traces[1:]:
        if out.itl_knee_c is None and t.itl_ratio is not None and t.itl_ratio > latency_factor:
            out.itl_knee_c = t.concurrency
        if out.ttft_knee_c is None and t.ttft_ratio is not None and t.ttft_ratio > latency_factor:
            out.ttft_knee_c = t.concurrency

    out.usl = fit_usl([(t.concurrency, t.throughput) for t in traces  # type: ignore[misc]
                       if t.throughput is not None])

    slo_active = {k: v for k, v in slo.items() if v is not None}
    if slo_active:
        max_ok = None
        for t in traces:
            ok = True
            if "ttft_p99" in slo_active:
                v = t.ttft
                if v is None or v > slo_active["ttft_p99"]:
                    ok = False
            if ok and "tpot_mean" in slo_active:
                v = t.tpot
                if v is None or v > slo_active["tpot_mean"]:
                    ok = False
            if ok and "itl_p95" in slo_active:
                v = t.itl
                if v is None or v > slo_active["itl_p95"]:
                    ok = False
            if ok and "e2e_p99" in slo_active:
                v = t.e2e
                if v is None or v > slo_active["e2e_p99"]:
                    ok = False
            if ok:
                max_ok = t.concurrency
            else:
                break
        out.slo_max_c = max_ok

    candidates: list[tuple[str, float]] = []
    for name, val in (
        ("plateau", out.plateau_c),
        ("peak", out.peak_c),
        ("kneedle", out.kneedle_c),
        ("itl_knee", out.itl_knee_c),
        ("ttft_knee", out.ttft_knee_c),
        ("slo", out.slo_max_c),
        ("retrograde", out.retrograde_c),
    ):
        if val is not None:
            candidates.append((name, val))
    if out.usl and out.usl.get("ok") and out.usl.get("n_star"):
        candidates.append(("usl_n_star", float(out.usl["n_star"])))

    if candidates:
        if len(candidates) == 1 and candidates[0][0] == "peak" and len(traces) < 3:
            out.notes.append("too few points to declare wall (only peak)")
        else:
            name, wall = min(candidates, key=lambda p: p[1])
            out.wall_c = wall
            out.notes.append(f"wall driven by {name}")
            target = wall * headroom
            measured = [t.concurrency for t in traces if t.concurrency <= wall]
            if measured:
                under = [c for c in measured if c <= target]
                out.recommended_c = max(under) if under else min(measured)
            else:
                out.recommended_c = max(1, int(round(target)))

    out.bottleneck, out.confidence = classify_bottleneck(out)
    return out


def classify_bottleneck(a: LengthAnalysis) -> tuple[str, str]:
    notes_extra = []
    comps = [t.completed for t in a.points if t.completed is not None]
    if len(comps) >= 2 and comps[-1] < comps[0] * 0.9:
        return "hard_overload", "high"

    itl_k = a.itl_knee_c
    ttft_k = a.ttft_knee_c
    plateau = a.plateau_c
    retro = a.retrograde_c
    usl = a.usl or {}

    if (
        plateau is not None
        and ttft_k is not None
        and (itl_k is None or ttft_k < itl_k)
        and a.peak_tp is not None
    ):
        late = [t for t in a.points if t.concurrency >= plateau]
        if late and all((t.gain is None or t.gain < 0.05) for t in late[1:]):
            if itl_k is None or (itl_k is not None and itl_k > plateau * 1.5):
                notes_extra.append("ttft spike with flat TP")
                a.notes.extend(notes_extra)
                return "client_bias_suspect", "medium"

    if retro is not None and plateau is not None:
        if itl_k is None or itl_k > retro:
            return "kv_or_memory", "medium"

    if plateau is not None and itl_k is not None and abs(plateau - itl_k) / max(plateau, 1) < 0.5:
        return "queue_buildup", "high" if a.n_points >= 5 else "medium"

    if ttft_k is not None and (itl_k is None or ttft_k < itl_k) and (
        plateau is None or plateau > ttft_k
    ):
        return "prefill_bound", "medium"

    if itl_k is not None and (ttft_k is None or itl_k <= ttft_k):
        return "decode_bound", "medium"

    if usl.get("ok") and usl.get("beta", 0) > 1e-6 and usl.get("n_star"):
        max_c = max(t.concurrency for t in a.points)
        if usl["n_star"] < max_c * 0.5:
            return "coherence_overhead", "medium"

    if a.n_points < 3:
        return "inconclusive", "low"

    if a.wall_c is not None and a.n_points >= 5:
        return "queue_buildup", "medium"

    conf = "low"
    if a.n_points >= 5 and a.wall_c is not None:
        conf = "medium"
    if a.n_points >= 5 and plateau and itl_k and abs(plateau - itl_k) <= max(plateau, itl_k) * 0.25:
        conf = "high"
    return ("inconclusive" if a.wall_c is None else "queue_buildup"), conf


# ══════════════════════════════════════════════════════════════════════
# 文档形态适配
# ══════════════════════════════════════════════════════════════════════

def _run_meta(doc: dict) -> dict[str, Any]:
    """从 schema.row_to_doc 文档提取分析用元信息（含 PD 分支）。"""
    params = doc.get("params") or {}
    deployment = doc.get("deployment_mode") or "colocated"
    tp = params.get("tp")
    dp = params.get("dp")
    pp = params.get("pp")
    if deployment == "pd_disagg":
        pd = doc.get("pd") or {}
        pf = (pd.get("prefill") or {}).get("params") or {}
        dc = (pd.get("decode") or {}).get("params") or {}
        # 展示：prefill/decode 并行度分别可读；聚合字段优先用文档顶层 gpu_count
        tp = tp if tp is not None else pf.get("tp") or dc.get("tp")
        dp = dp if dp is not None else pf.get("dp") or dc.get("dp")
        pp = pp if pp is not None else pf.get("pp") or dc.get("pp")

    run_id = doc.get("run_id") or doc.get("_id")
    return {
        "run_id": run_id,
        "model": doc.get("model"),
        "model_version": doc.get("model_version"),
        "gpu_type": doc.get("gpu_type"),
        "framework": doc.get("framework"),
        "framework_version": doc.get("framework_version"),
        "deployment_mode": deployment,
        "bench_framework": doc.get("bench_framework"),
        "bench_flush_cache": doc.get("bench_flush_cache"),
        "prefix_rate": doc.get("prefix_rate") if doc.get("prefix_rate") is not None else 0,
        "tp": tp,
        "dp": dp,
        "pp": pp,
        "gpu_count": doc.get("gpu_count"),
    }


def analyze_run(doc: dict, **kwargs) -> dict:
    """分析单条 run（文档形态）；kwargs 传给 analyze_length。"""
    meta = _run_meta(doc)
    groups = group_by_input_length(doc.get("metrics") or [])
    lengths = [
        analyze_length(il, rows, **kwargs)
        for il, rows in groups.items()
    ]
    return {
        **meta,
        "n_metric_rows": len(doc.get("metrics") or []),
        "n_input_lengths": len(lengths),
        "by_input_length": lengths,
    }


# ══════════════════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════════════════

def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v - round(v)) < 1e-6 and abs(v) >= 1:
            return str(int(round(v)))
        return f"{v:.{nd}f}"
    return str(v)


def _wall_metrics(la: LengthAnalysis) -> tuple[float | None, float | None]:
    wall_tp = None
    wall_itl = None
    if la.wall_c is None:
        return wall_tp, wall_itl
    for t in la.points:
        if t.concurrency == la.wall_c or abs(t.concurrency - la.wall_c) < 1e-6:
            return t.throughput, t.itl
    under = [t for t in la.points if t.concurrency <= la.wall_c]
    if under:
        return under[-1].throughput, under[-1].itl
    return wall_tp, wall_itl


def render_markdown(
    results: list[dict],
    slo: dict,
    *,
    include_points: bool = True,
    caveats: list[str] | None = None,
) -> str:
    """渲染 Markdown。include_points=False 时只出汇总表（给 LLM 上下文用）。"""
    lines: list[str] = ["# Hardware Wall 分析结果", ""]
    if any(slo.values()):
        lines.append("## SLO")
        for k, v in slo.items():
            if v is not None:
                lines.append(f"- `{k}` ≤ {v} ms")
        lines.append("")

    for res in results:
        lines.append(f"## run `{res['run_id']}`")
        lines.append("")
        lines.append(
            f"- model=`{res['model']}` gpu=`{res['gpu_type']}` "
            f"framework=`{res['framework']}` "
            f"tp={res.get('tp')} dp={res.get('dp')} pp={res.get('pp')}"
        )
        if res.get("gpu_count") is not None:
            lines.append(f"- gpu_count=`{res.get('gpu_count')}` deployment=`{res.get('deployment_mode')}`")
        lines.append(
            f"- bench_framework=`{res.get('bench_framework')}` "
            f"flush=`{res.get('bench_flush_cache')}` "
            f"prefix_rate=`{res.get('prefix_rate')}`"
        )
        lines.append(
            f"- metrics={res['n_metric_rows']} rows, "
            f"{res['n_input_lengths']} input_lengths"
        )
        lines.append("")
        lines.append(
            "| input_length | wall并发 | 推荐运行点 | 峰值吞吐 | "
            "wall处吞吐 | wall处ITL | USL N* | 瓶颈 | 置信度 |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---|---|")

        for la in res["by_input_length"]:
            wall_tp, wall_itl = _wall_metrics(la)
            n_star = None
            if la.usl and la.usl.get("ok"):
                n_star = la.usl.get("n_star")
            lines.append(
                f"| {_fmt(la.input_length, 0)} | {_fmt(la.wall_c, 1)} | "
                f"{_fmt(la.recommended_c, 1)} | {_fmt(la.peak_tp)} | "
                f"{_fmt(wall_tp)} | {_fmt(wall_itl)} | {_fmt(n_star, 1)} | "
                f"{la.bottleneck} | {la.confidence} |"
            )

        lines.append("")

        if include_points:
            for la in res["by_input_length"]:
                lines.append(f"### input_length = {_fmt(la.input_length, 0)}")
                lines.append("")
                lines.append(
                    f"- candidates: plateau={_fmt(la.plateau_c)} peak={_fmt(la.peak_c)} "
                    f"kneedle={_fmt(la.kneedle_c)} itl_knee={_fmt(la.itl_knee_c)} "
                    f"ttft_knee={_fmt(la.ttft_knee_c)} slo_max={_fmt(la.slo_max_c)} "
                    f"retrograde={_fmt(la.retrograde_c)}"
                )
                if la.usl:
                    if la.usl.get("ok"):
                        lines.append(
                            f"- USL: α={_fmt(la.usl.get('alpha'), 4)} "
                            f"β={_fmt(la.usl.get('beta'), 6)} "
                            f"γ={_fmt(la.usl.get('gamma'))} "
                            f"N*={_fmt(la.usl.get('n_star'), 1)} "
                            f"Xmax={_fmt(la.usl.get('x_max'))}"
                        )
                    else:
                        lines.append(f"- USL: skipped ({la.usl.get('reason')})")
                if la.notes:
                    lines.append(f"- notes: {'; '.join(la.notes)}")
                lines.append("")
                lines.append(
                    "| concurrency | throughput | gain | ITL | ITL× | TTFT | TTFT× | "
                    "Little's L | completed |"
                )
                lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
                for t in la.points:
                    lines.append(
                        f"| {_fmt(t.concurrency, 0)} | {_fmt(t.throughput)} | "
                        f"{_fmt(t.gain, 3)} | {_fmt(t.itl)} | {_fmt(t.itl_ratio, 2)} | "
                        f"{_fmt(t.ttft)} | {_fmt(t.ttft_ratio, 2)} | "
                        f"{_fmt(t.littles_L, 1)} | {_fmt(t.completed, 0)} |"
                    )
                lines.append("")
                lines.append(
                    f"**结论**：墙 ≈ c={_fmt(la.wall_c)}；推荐 ≤ {_fmt(la.recommended_c)}；"
                    f"瓶颈=`{la.bottleneck}`；置信度=`{la.confidence}`。"
                )
                lines.append("")
        else:
            for la in res["by_input_length"]:
                lines.append(
                    f"- **il={_fmt(la.input_length, 0)}**：墙≈c={_fmt(la.wall_c)}；"
                    f"推荐≤{_fmt(la.recommended_c)}；瓶颈=`{la.bottleneck}`；"
                    f"置信度=`{la.confidence}`"
                    + (f"；notes: {'; '.join(la.notes)}" if la.notes else "")
                )
            lines.append("")

    if caveats:
        lines.append("## 陷阱（本次触发）")
        for i, c in enumerate(caveats, 1):
            lines.append(f"{i}. {c}")
        lines.append("")
    else:
        lines.append("## 陷阱自检")
        lines.append("1. 客户端偏置（GIL）— 见 `client_bias_suspect`")
        lines.append("2. 四指标不定根因 — 结合 params / 监控")
        lines.append("3. 饱和非模型属性 — 勿跨 dataset 混比")
        lines.append("4. 缓存混淆 — 核对 flush / prefix_rate")
        lines.append("5. PROMPTS∝concurrency — 高并发噪声大")
        lines.append("")
    return "\n".join(lines)


# 兼容旧名
render_md = render_markdown


def length_to_summary(la: LengthAnalysis, *, include_points: bool) -> dict:
    """LengthAnalysis → 可 JSON 化的摘要（默认不含逐点明细）。"""
    d: dict[str, Any] = {
        "input_length": la.input_length,
        "n_points": la.n_points,
        "wall_c": la.wall_c,
        "recommended_c": la.recommended_c,
        "peak_c": la.peak_c,
        "peak_tp": la.peak_tp,
        "candidates": {
            "plateau": la.plateau_c,
            "peak": la.peak_c,
            "kneedle": la.kneedle_c,
            "itl_knee": la.itl_knee_c,
            "ttft_knee": la.ttft_knee_c,
            "slo_max": la.slo_max_c,
            "retrograde": la.retrograde_c,
        },
        "usl": la.usl,
        "bottleneck": la.bottleneck,
        "confidence": la.confidence,
        "notes": list(la.notes),
    }
    wall_tp, wall_itl = _wall_metrics(la)
    d["wall_throughput"] = wall_tp
    d["wall_itl"] = wall_itl
    if include_points:
        d["points"] = [asdict(t) for t in la.points]
    return d


def to_jsonable(results: list[dict], *, include_points: bool = True) -> list[dict]:
    out = []
    for res in results:
        r = {k: v for k, v in res.items() if k != "by_input_length"}
        r["by_input_length"] = [
            length_to_summary(la, include_points=include_points)
            for la in res["by_input_length"]
        ]
        out.append(r)
    return out


def collect_caveats(results: list[dict]) -> list[str]:
    """按数据触发附加陷阱说明。"""
    caveats: list[str] = []
    seen: set[str] = set()

    def add(key: str, text: str) -> None:
        if key not in seen:
            seen.add(key)
            caveats.append(text)

    for res in results:
        flush = res.get("bench_flush_cache")
        pr = res.get("prefix_rate") or 0
        try:
            pr_f = float(pr)
        except (TypeError, ValueError):
            pr_f = 0.0
        if flush is False and pr_f > 0:
            add(
                "cache",
                "缓存混淆：bench_flush_cache=false 且 prefix_rate>0，吞吐可能虚高，不可与冷启动混比。",
            )
        for la in res["by_input_length"]:
            if la.bottleneck == "client_bias_suspect":
                add(
                    "client",
                    "客户端偏置嫌疑：吞吐已平台而 TTFT 暴涨、ITL 未同步——可能是 bench 客户端 GIL，非 server 墙。",
                )
            if la.n_points < 3:
                add(
                    "few_points",
                    "部分 input_length 并发扫描点 < 3，墙点置信度低或 inconclusive。",
                )
    return caveats


# ══════════════════════════════════════════════════════════════════════
# 服务入口
# ══════════════════════════════════════════════════════════════════════

def analyze_saturation_runs(
    db,
    *,
    filters: dict | None = None,
    exclude: dict | None = None,
    run_id: str | None = None,
    slo: dict[str, float | None] | None = None,
    plateau_gain: float = 0.10,
    latency_factor: float = 2.0,
    headroom: float = 0.8,
    retro_tol: float = 0.05,
    include_points: bool = False,
    max_runs: int = DEFAULT_MAX_RUNS,
) -> dict:
    """
    服务级入口：按维度条件取 run → 分析 → 返回 JSON + Markdown。

    命中 0 条 → ok=false；命中 > max_runs → ok=false 提示加约束。
    """
    filters = filters or {}
    exclude = exclude or {}

    for dim in list(filters) + list(exclude):
        if dim not in schema.ALL_DIMENSIONS:
            return {
                "ok": False,
                "error": f"未知维度: {dim}",
                "valid_dimensions": list(schema.ALL_DIMENSIONS),
            }

    where_sql, params = build_conditions(filters, exclude)
    if run_id:
        clause = "run_id = ?"
        where_sql = f"{where_sql} AND {clause}" if where_sql else clause
        params = list(params) + [run_id]

    docs = db.fetch_runs(where_sql, params)
    n = len(docs)
    if n == 0:
        return {
            "ok": False,
            "reason": "命中 0 条记录，无法分析",
            "n_matched": 0,
        }
    if n > max_runs:
        preview = []
        for d in docs[:20]:
            preview.append({
                "run_id": d.get("_id"),
                "model": d.get("model"),
                "gpu_type": d.get("gpu_type"),
                "framework": d.get("framework"),
                "framework_version": d.get("framework_version"),
            })
        return {
            "ok": False,
            "reason": f"命中 {n} 条，超过上限 {max_runs}，请加 filters/exclude 或指定 run_id",
            "n_matched": n,
            "preview": preview,
        }

    slo_map: dict[str, float | None] = {
        "ttft_p99": None,
        "tpot_mean": None,
        "itl_p95": None,
        "e2e_p99": None,
    }
    if slo:
        for k in slo_map:
            if k in slo and slo[k] is not None:
                slo_map[k] = float(slo[k])

    kwargs = dict(
        plateau_gain=plateau_gain,
        latency_factor=latency_factor,
        headroom=headroom,
        retro_tol=retro_tol,
        slo=slo_map,
    )
    results = [analyze_run(d, **kwargs) for d in docs]
    caveats = collect_caveats(results)
    markdown = render_markdown(
        results, slo_map, include_points=include_points, caveats=caveats or None,
    )
    return {
        "ok": True,
        "n_runs": len(results),
        "settings": {
            "plateau_gain": plateau_gain,
            "latency_factor": latency_factor,
            "headroom": headroom,
            "retro_tol": retro_tol,
            "slo": {k: v for k, v in slo_map.items() if v is not None},
            "include_points": include_points,
        },
        "runs": to_jsonable(results, include_points=include_points),
        "markdown": markdown,
        "caveats": caveats,
    }
