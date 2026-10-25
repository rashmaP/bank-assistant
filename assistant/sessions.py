from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class Session:
    id: str
    customer_id: str
    turns: list[list[dict]] = field(default_factory=list)  # each turn = user msg + tool/assistant msgs
    turn_no: int = 0
    pending_cheque: dict | None = None  # confirmation gate state
    grounded_text: str = ""  # everything the bank has told us this session (for the grounding check)
    cheque_requests_raised: list[str] = field(default_factory=list)
    last_seen: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def messages(self) -> list[dict]:
        return [m for turn in self.turns for m in turn]


class SessionStore:
    """In-memory store. Production: Redis/DB with TTL; sessions are bound to a customer."""

    def __init__(self, ttl_seconds: int = 1800):
        self._s: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def get_or_create(self, session_id: str | None, customer_id: str) -> Session:
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._s.items() if now - v.last_seen > self._ttl]:
                del self._s[k]
            if session_id and session_id in self._s:
                s = self._s[session_id]
                if s.customer_id != customer_id:
                    raise PermissionError("session belongs to another customer")
                s.last_seen = now
                return s
            s = Session(id=uuid.uuid4().hex, customer_id=customer_id)  # never trust client-chosen ids
            self._s[s.id] = s
            return s


class RateLimiter:
    def __init__(self, per_minute: int):
        self._n, self._hits, self._lock = per_minute, defaultdict(deque), threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self._n:
                return False
            q.append(now)
            return True
