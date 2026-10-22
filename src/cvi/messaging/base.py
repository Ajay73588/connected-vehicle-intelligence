"""Broker abstraction: Kafka in production, an in-memory broker for tests/single-process demos.

Semantics required by the pipeline (and provided by both implementations):
- keyed partitioning (same key -> same partition -> per-key ordering)
- consumer groups with explicit offset commits (at-least-once delivery; replay on restart)
- pause/resume for back-pressure, lag reporting, dead-letter topic by convention."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

TOPIC_RAW = "telemetry.raw"
TOPIC_VALIDATED = "telemetry.validated"
TOPIC_DLQ = "telemetry.dlq"
TOPIC_ALERTS = "alerts"
ALL_TOPICS = (TOPIC_RAW, TOPIC_VALIDATED, TOPIC_DLQ, TOPIC_ALERTS)


@dataclass
class Message:
    topic: str
    key: str | None
    value: bytes
    partition: int = 0
    offset: int = 0
    headers: dict[str, str] = field(default_factory=dict)


class Consumer(Protocol):
    def poll_batch(self, max_messages: int, timeout_s: float) -> list[Message]: ...
    def commit(self, messages: list[Message]) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def lag(self) -> dict[str, int]: ...
    def close(self) -> None: ...


class Broker(Protocol):
    def produce(self, topic: str, key: str | None, value: bytes, headers: dict[str, str] | None = None) -> None: ...
    def flush(self, timeout_s: float = 10.0) -> int: ...
    def subscribe(self, group: str, topics: list[str]) -> Consumer: ...
    def ensure_topics(self) -> None: ...
    def healthy(self) -> bool: ...
