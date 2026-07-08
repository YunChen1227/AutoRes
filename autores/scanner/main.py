"""
Scanner 主循环（进程 A，design.md §5.5）。

每轮：发现待处理目录 → 逐个解析入库 → 成功写 ingest_log；失败跳过、下轮重试。
单节点、无事务，靠 _id 幂等去重。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from autores.config import get_config
from autores.common.logging import setup_logging, get_logger
from autores.db import client as dbc
from autores.scanner import discovery
from autores.scanner.parser import parse_run_dir, ParseError

log = get_logger("scanner")


def _mark_ingested(db, dir_name: str, run_id: str) -> None:
    dbc.ingest_log(db).update_one(
        {"_id": dir_name},
        {"$set": {"ingested_at": datetime.now(timezone.utc), "run_id": run_id}},
        upsert=True,
    )


def ingest_dir(db, root: str, dir_name: str) -> bool:
    """
    解析并入库单个目录。返回 True 表示已入库（或已存在），False 表示失败。
    """
    dir_path = os.path.join(root, dir_name)
    try:
        doc = parse_run_dir(dir_path)
    except ParseError as e:
        log.warning("解析失败，跳过（下轮重试）", extra={"fields": {"dir": dir_name, "error": str(e)}})
        return False

    try:
        dbc.test_runs(db).insert_one(doc)
    except DuplicateKeyError:
        # 崩溃边界（§6.2）：test_runs 已写、ingest_log 没写。视为已入库，补写台账。
        log.info("文档已存在，补写台账", extra={"fields": {"dir": dir_name}})
        _mark_ingested(db, dir_name, doc["_id"])
        return True
    except Exception as e:  # noqa: BLE001 —— 其余异常（网络等）也跳过、下轮重试
        log.error("入库失败，跳过（下轮重试）", extra={"fields": {"dir": dir_name, "error": str(e)}})
        return False

    _mark_ingested(db, dir_name, doc["_id"])
    log.info("入库成功", extra={"fields": {"dir": dir_name, "metrics": len(doc["metrics"])}})
    return True


def scan_once(db, root: str, pattern: str) -> tuple[int, int, int]:
    """扫描一轮。返回 (待处理数, 成功数, 失败数)。"""
    pending = discovery.find_pending(db, root, pattern)
    ok = fail = 0
    for dir_name in pending:
        if ingest_dir(db, root, dir_name):
            ok += 1
        else:
            fail += 1
    if pending:
        log.info("本轮扫描完成", extra={"fields": {"pending": len(pending), "ok": ok, "fail": fail}})
    return len(pending), ok, fail


def run() -> None:
    setup_logging()
    cfg = get_config()
    db = dbc.connect(cfg.database, ensure_indexes=True)
    root = cfg.scanner.benchmark_root
    pattern = cfg.scanner.dir_pattern
    interval = cfg.scanner.interval_seconds

    log.info("Scanner 启动", extra={"fields": {"root": root, "interval": interval}})
    while True:
        try:
            scan_once(db, root, pattern)
        except Exception as e:  # noqa: BLE001 —— 单轮异常不退出进程
            log.error("扫描轮异常", extra={"fields": {"error": str(e)}})
        time.sleep(interval)


if __name__ == "__main__":
    run()
