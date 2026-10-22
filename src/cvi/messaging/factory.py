from __future__ import annotations

from cvi.common.config import get_settings
from cvi.messaging.base import Broker
from cvi.messaging.memory import MemoryBroker

_memory_singleton: MemoryBroker | None = None


def get_broker() -> Broker:
    global _memory_singleton
    s = get_settings()
    if s.broker == "memory":
        if _memory_singleton is None:
            _memory_singleton = MemoryBroker(s.topic_partitions)
        return _memory_singleton
    from cvi.messaging.kafka import KafkaBroker

    return KafkaBroker()
