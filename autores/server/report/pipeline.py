"""报告流水线编排：QuerySpec → 查询 → 取最新 → 对齐 → Excel（design.md §7.4）。"""
from __future__ import annotations

from dataclasses import dataclass

from autores.server.report import align, excel, hardware
from autores.server.report.query import QuerySpec, run_query

# 按 kind 决定「拆 sheet 的行键」：text 每个 prefix_rate 一个 sheet；
# vlm 维度多（image_count/video_count/image_resolution），拆开会 sheet 爆炸，暂维持单 sheet。
_SHEET_DIMS_BY_KIND: dict[str, list[str]] = {"text": ["prefix_rate"]}


@dataclass
class ReportResult:
    file_path: str
    num_runs: int
    num_metric_rows: int
    column_labels: list
    notes: dict
    empty: bool = False


def generate_report(db, spec: QuerySpec, output_dir: str) -> ReportResult:
    """执行完整流水线。命中 0 条时返回 empty=True，不生成文件。"""
    docs = run_query(db, spec)
    if not docs:
        return ReportResult(
            file_path="", num_runs=0, num_metric_rows=0,
            column_labels=[], notes={}, empty=True,
        )

    docs = align.merge_duplicates(docs, kind=spec.benchmark_kind)

    gpu_scaled = False
    if spec.normalize_gpu_scale:
        gpu_scaled = hardware.annotate_and_scale(docs)

    table = align.build_comparison_table(
        docs,
        compare_on=spec.compare_on,
        metrics=spec.metrics,
        metric_filters=spec.metric_filters,
        gpu_scaled=gpu_scaled,
        kind=spec.benchmark_kind,
    )
    # text kind：按 prefix_rate 拆 sheet（每个前缀比例一个 sheet，sheet 集合取并集，
    # 缺该前缀的副本整列 N/A，两边都有才对比）。其余 kind 维持单 sheet。
    sheet_dims = _SHEET_DIMS_BY_KIND.get(spec.benchmark_kind, [])
    if sheet_dims:
        table = align.split_table_into_sheets(table, sheet_dims)
    path = excel.render_comparison(table, spec.compare_on, output_dir)

    return ReportResult(
        file_path=path,
        num_runs=len(docs),
        num_metric_rows=len(table["rows"]),
        column_labels=table["column_labels"],
        notes=table["notes"],
        empty=False,
    )
