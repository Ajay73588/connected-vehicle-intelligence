"""In-memory partitioned log with consumer-group offsets. Faithful to the Kafka semantics the pipeline relies on
(keyed partitions, uncommitted messages are redelivered to a new consumer of the same group)."""

from __future__ import annotations

import threading
import time
import zlib
from collections import defaultdict

from cvi.messaging.base import ALL_TOPICS, Message


class MemoryBroker:
    def __init__(self, partitions: int = 4):
        self.partitions = partitions
        self._log: dict[str, list[list[Message]]] = defaultdict(lambda: [[] for _ in range(partitions)])
        self._committed: dict[tuple[str, str, int], int] = {}  # (group, topic, partition) -> next offset
        self._lock = threading.RLock()
        self.down = False  # chaos switch: simulates a broker outage

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("broker unavailable")

    def ensure_topics(self) -> None:
        for t in ALL_TOPICS:
            _ = self._log[t]

    def healthy(self) -> bool:
        return not self.down

    def produce(self, topic: str, key: str | None, value: bytes, headers: dict[str, str] | None = None) -> None:
        self._check()
        with self._lock:
            parts = self._log[topic]
            p = zlib.crc32(key.encode()) % self.partitions if key else len(parts[0]) % self.partitions
            parts[p].append(Message(topic, key, value, p, len(parts[p]), headers or {}))

    def flush(self, timeout_s: float = 10.0) -> int:
        return 0

    def subscribe(self, group: str, topics: list[str]) -> MemoryConsumer:
        return MemoryConsumer(self, group, topics)

    # test helper
    def depth(self, topic: str) -> int:
        return sum(len(p) for p in self._log[topic])

    def messages(self, topic: str) -> list[Message]:
        return [m for p in self._log[topic] for m in p]


class MemoryConsumer:
    def __init__(self, broker: MemoryBroker, group: str, topics: list[str]):
        self.b, self.group, self.topics = broker, group, topics
        self._pos: dict[tuple[str, int], int] = {}
        self._paused = False
        self.closed = False

    def _position(self, topic: str, p: int) -> int:
        return self._pos.get((topic, p), self.b._committed.get((self.group, topic, p), 0))

    def poll_batch(self, max_messages: int, timeout_s: float) -> list[Message]:
        self.b._check()
        if self._paused or self.closed:
            return []
        out: list[Message] = []
        with self.b._lock:
            for t in self.topics:
                for p, part in enumerate(self.b._log[t]):
                    pos = self._position(t, p)
                    take = part[pos : pos + (max_messages - len(out))]
                    if take:
                        self._pos[(t, p)] = pos + len(take)
                        out.extend(take)
                    if len(out) >= max_messages:
                        return out
        if not out and timeout_s > 0:
            time.sleep(min(timeout_s, 0.02))  # behave like a blocking Kafka poll instead of spinning the CPU
        return out

    def commit(self, messages: list[Message]) -> None:
        self.b._check()
        with self.b._lock:
            for m in messages:
                k = (self.group, m.topic, m.partition)
                self.b._committed[k] = max(self.b._committed.get(k, 0), m.offset + 1)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def lag(self) -> dict[str, int]:
        with self.b._lock:
            return {
                t: sum(len(part) - self.b._committed.get((self.group, t, p), 0) for p, part in enumerate(self.b._log[t]))
                for t in self.topics
            }

    def close(self) -> None:
        self.closed = True
