"""
SQLite 访问层。Scanner 与 API 共用同一 Database 类。

单节点、量小、Scanner 单进程写入；连接开 WAL 模式支持"一写多读"，
进程内用锁串行化写操作。所有 SQL 集中在本模块，上层只操作文档 dict。

表名一律经 schema.table_for(kind) 解析，禁止外部字符串拼接。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

from autores.config import DatabaseConfig
from autores.db import schema

DuplicateRunError = sqlite3.IntegrityError  # 主键冲突（幂等去重信号，§6.2）


class Database:
    def __init__(self, path: str):
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False：API 侧 handler 可能跑在线程池，用 _lock 串行化
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            # 老库：CREATE TABLE 跳过已有表 → migrate 补列 → 再建索引（否则缺列报错）
            self._conn.executescript(schema.DDL_TABLES)
            schema.migrate(self._conn)
            self._conn.executescript(schema.DDL_INDEXES)
            self._conn.commit()

    # ── 基础 ──

    def ping(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── runs（按 benchmark_kind 路由到 test_runs / vlm_test_runs）──

    def insert_run(self, doc: dict, kind: str | None = None) -> None:
        """插入一次测试。run_id 冲突抛 DuplicateRunError（sqlite3.IntegrityError）。"""
        bk = schema.resolve_kind(kind or doc.get("benchmark_kind")).name
        table = schema.table_for(bk)
        row = schema.doc_to_row(doc, bk)
        cols = list(row.keys())
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) "
               f"VALUES ({', '.join('?' for _ in cols)})")
        with self._lock:
            self._conn.execute(sql, [row[c] for c in cols])
            self._conn.commit()

    def fetch_runs(self, where_sql: str = "", params: list | None = None,
                   kind: str | None = None) -> list[dict]:
        """按条件取测试记录，返回文档形态列表（含 metrics）。"""
        bk = schema.resolve_kind(kind).name
        table = schema.table_for(bk)
        sql = f"SELECT * FROM {table}"
        if where_sql:
            sql += f" WHERE {where_sql}"
        with self._lock:
            rows = self._conn.execute(sql, params or []).fetchall()
        return [schema.row_to_doc(r, bk) for r in rows]

    def count_runs(self, where_sql: str = "", params: list | None = None,
                   kind: str | None = None) -> int:
        table = schema.table_for(kind)
        sql = f"SELECT COUNT(*) FROM {table}"
        if where_sql:
            sql += f" WHERE {where_sql}"
        with self._lock:
            return self._conn.execute(sql, params or []).fetchone()[0]

    def dimension_values(self, dimension: str,
                         where_sql: str = "", params: list | None = None,
                         kind: str | None = None) -> list[dict]:
        """某维度的去重取值及记录数，按记录数降序。"""
        table = schema.table_for(kind)
        col = schema.dimension_column(dimension)
        sql = f"SELECT {col} AS value, COUNT(*) AS cnt FROM {table}"
        if where_sql:
            sql += f" WHERE {where_sql}"
        sql += f" GROUP BY {col} ORDER BY cnt DESC"
        with self._lock:
            rows = self._conn.execute(sql, params or []).fetchall()
        out = []
        for r in rows:
            val = r["value"]
            if dimension in schema.BOOL_PARAMS and val is not None:
                val = bool(val)
            out.append({"value": val, "count": r["cnt"]})
        return out

    def group_counts(self, dimensions: list[str],
                     where_sql: str = "", params: list | None = None,
                     kind: str | None = None) -> list[dict]:
        """
        按一个或多个维度分组统计记录数。
        返回 [{dim1: v1, dim2: v2, ..., "count": n}, ...]，按 count 降序。
        """
        table = schema.table_for(kind)
        cols = [schema.dimension_column(d) for d in dimensions]
        col_list = ", ".join(cols)
        sql = f"SELECT {col_list}, COUNT(*) AS cnt FROM {table}"
        if where_sql:
            sql += f" WHERE {where_sql}"
        sql += f" GROUP BY {col_list} ORDER BY cnt DESC"
        with self._lock:
            rows = self._conn.execute(sql, params or []).fetchall()
        out = []
        for r in rows:
            item = {}
            for dim, col in zip(dimensions, cols):
                val = r[col]
                if dim in schema.BOOL_PARAMS and val is not None:
                    val = bool(val)
                item[dim] = val
            item["count"] = r["cnt"]
            out.append(item)
        return out

    # ── ingest_log ──

    def ingested_dirs(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT source_dir FROM ingest_log").fetchall()
        return {r["source_dir"] for r in rows}

    def mark_ingested(self, source_dir: str, run_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ingest_log (source_dir, run_id, ingested_at) "
                "VALUES (?, ?, ?)",
                [source_dir, run_id, datetime.now(timezone.utc).isoformat()],
            )
            self._conn.commit()


def connect(cfg: DatabaseConfig) -> Database:
    """建库（文件不存在自动创建）、建表建索引（幂等），返回 Database 句柄。"""
    return Database(cfg.path)
