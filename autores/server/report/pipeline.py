"""报告流水线编排：QuerySpec → 查询 → 取最新 → 对齐 → Excel（design.md §7.4）。"""
from __future__ import annotations

from dataclasses import dataclass

from autores.server.report import align, excel
from autores.server.report.query import QuerySpec, run_query


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

    docs = align.take_latest(docs)
    table = align.build_comparison_table(
        docs,
        compare_on=spec.compare_on,
        metrics=spec.metrics,
        metric_filters=spec.metric_filters,
    )
    path = excel.render_comparison(table, spec.compare_on, output_dir)

    return ReportResult(
        file_path=path,
        num_runs=len(docs),
        num_metric_rows=len(table["rows"]),
        column_labels=table["column_labels"],
        notes=table["notes"],
        empty=False,
    )
