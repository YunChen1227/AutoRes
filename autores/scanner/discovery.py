"""目录发现：列出 NAS 上时间戳目录，与 ingest_log 台账比对得出待处理集合。"""
from __future__ import annotations

import os
import re

from autores.db import client as dbc


def list_timestamp_dirs(root: str, pattern: str) -> list[str]:
    """列出 root 下所有匹配时间戳格式的一级子目录名（不含路径）。"""
    if not os.path.isdir(root):
        return []
    rx = re.compile(pattern)
    names = []
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if os.path.isdir(full) and rx.match(name):
            names.append(name)
    return sorted(names)


def already_ingested(db) -> set[str]:
    """查 ingest_log，返回已成功入库的目录名集合。"""
    return {doc["_id"] for doc in dbc.ingest_log(db).find({}, {"_id": 1})}


def find_pending(db, root: str, pattern: str) -> list[str]:
    """待处理 = 磁盘上所有时间戳目录 − 已入库台账（design.md §6.2）。"""
    on_disk = list_timestamp_dirs(root, pattern)
    ingested = already_ingested(db)
    return [name for name in on_disk if name not in ingested]
