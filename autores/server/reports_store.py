"""报告文件注册表：token → 文件路径映射（内存），带 TTL 清理（design.md §7.6）。"""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass


@dataclass
class _Entry:
    path: str
    created_at: float


class ReportStore:
    def __init__(self, ttl_minutes: int):
        self.ttl_seconds = ttl_minutes * 60
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def register(self, file_path: str) -> tuple[str, str]:
        """登记一个报告文件，返回 (download_url, filename)。"""
        token = uuid.uuid4().hex
        with self._lock:
            self._entries[token] = _Entry(path=file_path, created_at=time.time())
        filename = os.path.basename(file_path)
        return f"/api/download/{token}", filename

    def resolve(self, token: str) -> str | None:
        """token → 文件路径。过期或不存在返回 None。"""
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return None
            if time.time() - entry.created_at > self.ttl_seconds:
                del self._entries[token]
                return None
            return entry.path

    def cleanup(self) -> int:
        """删除过期报告文件及其映射。返回清理数量。"""
        now = time.time()
        removed = 0
        with self._lock:
            expired = [(t, e) for t, e in self._entries.items()
                       if now - e.created_at > self.ttl_seconds]
            for token, entry in expired:
                try:
                    if os.path.exists(entry.path):
                        os.remove(entry.path)
                except OSError:
                    pass
                del self._entries[token]
                removed += 1
        return removed
