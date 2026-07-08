"""MongoDB 连接（PyMongo 同步）。Scanner 与 API 共用。"""
from __future__ import annotations

from pymongo import MongoClient
from pymongo.database import Database

from autores.config import DatabaseConfig
from autores.db import schema


def connect(cfg: DatabaseConfig, ensure_indexes: bool = True) -> Database:
    """建立连接，返回 Database 句柄；可选确保索引存在。"""
    client = MongoClient(cfg.uri, tz_aware=True)
    db = client[cfg.db_name]
    if ensure_indexes:
        schema.build_indexes(db)
    return db


def test_runs(db: Database):
    return db[schema.COLLECTION_TEST_RUNS]


def ingest_log(db: Database):
    return db[schema.COLLECTION_INGEST_LOG]
