"""
Excel 渲染（design.md §7.4 步骤 4-5）。纯数据对比表，无图表、无结论。
"""
from __future__ import annotations

import os
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

_HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_TITLE_FONT = Font(bold=True, size=12)


def _constraints_text(constraints: dict) -> str:
    # 维度名 -> 中文展示
    label_map = {
        "model": "模型", "model_version": "模型版本",
        "framework": "框架", "framework_version": "框架版本",
        "gpu_type": "显卡", "tp": "TP", "dp": "DP", "pp": "PP",
        "ep": "EP", "cp": "CP", "kv_cache_dtype": "KV Cache Dtype",
        "hicache_enabled": "HiCache", "flexkv_enabled": "FlexKV",
        "torch_compile": "TorchCompile", "quantization": "量化",
        "attention_backend": "Attention后端",
    }
    parts = [f"{label_map.get(k, k)}: {v}" for k, v in constraints.items()]
    return " | ".join(parts)


def render_comparison(table: dict, compare_on: str, output_dir: str) -> str:
    """把对比宽表渲染为 xlsx，返回文件路径。"""
    os.makedirs(output_dir, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "对比报告"

    column_labels = table["column_labels"]
    rows = table["rows"]

    # 标题区
    title = f"对比轴: {compare_on}"
    constraints = _constraints_text(table["constraints"])
    ws.cell(row=1, column=1, value=title).font = _TITLE_FONT
    if constraints:
        ws.cell(row=2, column=1, value=f"约束: {constraints}")
    note_bits = []
    if table["notes"].get("multi_framework"):
        note_bits.append("含多个框架（版本号不可跨框架比较）")
    if table["notes"].get("multi_version"):
        note_bits.append("含多个框架版本（各自独立取出）")
    if note_bits:
        ws.cell(row=3, column=1, value="说明: " + "；".join(note_bits))

    header_row = 5

    # 表头：第一列是"指标"，其后是对比轴各取值
    ws.cell(row=header_row, column=1, value="指标 (输入长度/并发)")
    for j, col_label in enumerate(column_labels, start=2):
        ws.cell(row=header_row, column=j, value=str(col_label))
    for j in range(1, len(column_labels) + 2):
        c = ws.cell(row=header_row, column=j)
        c.fill = _HEADER_FILL
        c.font = _HEADER_FONT
        c.alignment = Alignment(horizontal="center")

    # 数据行
    for i, row in enumerate(rows, start=header_row + 1):
        ws.cell(row=i, column=1, value=row["label"])
        for j, col_label in enumerate(column_labels, start=2):
            ws.cell(row=i, column=j, value=row["values"].get(col_label, "N/A"))

    # 冻结首行首列（冻结表头行 + 指标列）
    ws.freeze_panes = ws.cell(row=header_row + 1, column=2)

    # 列宽自适应（简单估算）
    for j in range(1, len(column_labels) + 2):
        letter = get_column_letter(j)
        max_len = 0
        for i in range(header_row, header_row + 1 + len(rows)):
            v = ws.cell(row=i, column=j).value
            if v is not None:
                max_len = max(max_len, len(str(v)))
        ws.column_dimensions[letter].width = min(max(max_len + 2, 12), 48)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"对比报告_{compare_on}_{ts}.xlsx"
    path = os.path.join(output_dir, filename)
    wb.save(path)
    return path
