"""In-process WebSocket pub/sub. One process only: for several workers, put
Redis pub/sub (or Postgres LISTEN/NOTIFY) behind the same publish() interface."""
from __future__ import annotations

from collections import defaultdict, deque


class Hub:
    def __init__(self, recent_size: int = 200) -> None:
        self._subs: dict[str, set] = defaultdict(set)
        self._recent: dict[str, deque] = defaultdict(lambda: deque(maxlen=recent_size))

    def subscribe(self, topic: str, ws) -> None:
        self._subs[topic].add(ws)

    def unsubscribe(self, topic: str, ws) -> None:
        self._subs[topic].discard(ws)

    def count(self, topic: str) -> int:
        return len(self._subs[topic])

    def recent(self, topic: str) -> list[dict]:
        return list(self._recent[topic])

    async def publish(self, topic: str, message: dict) -> None:
        self._recent[topic].append(message)
        dead = []
        for ws in list(self._subs[topic]):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(topic, ws)
