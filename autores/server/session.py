"""内存会话管理（不持久化，design.md §7.5）。带 TTL 与消息数上限。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Session:
    session_id: str
    messages: list[dict] = field(default_factory=list)  # 含 system + 历史
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self, ttl_minutes: int, max_messages: int):
        self.ttl_seconds = ttl_minutes * 60
        self.max_messages = max_messages
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str, system_message: dict) -> Session:
        with self._lock:
            self._evict_expired_locked()
            sess = self._sessions.get(session_id)
            if sess is None:
                sess = Session(session_id=session_id, messages=[system_message])
                self._sessions[session_id] = sess
            sess.last_active = time.time()
            return sess

    def trim(self, sess: Session) -> None:
        """限制消息数：保留 system + 最近 max_messages 条。"""
        with self._lock:
            if len(sess.messages) <= self.max_messages + 1:
                return
            system = sess.messages[0]
            tail = sess.messages[-self.max_messages:]
            sess.messages = [system] + tail

    def _evict_expired_locked(self) -> None:
        now = time.time()
        expired = [sid for sid, s in self._sessions.items()
                   if now - s.last_active > self.ttl_seconds]
        for sid in expired:
            del self._sessions[sid]
