"""Kafka (or Redpanda) implementation on confluent-kafka (librdkafka).

Producer: idempotent, lz4-compressed, linger-batched.  Consumer: manual commits, cooperative rebalancing.
Covered by the integration tests / compose stack (needs a live broker)."""

from __future__ import annotations

import logging

from confluent_kafka import Consumer as KConsumer
from confluent_kafka import KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from cvi.common.config import get_settings
from cvi.messaging.base import ALL_TOPICS, Message

logger = logging.getLogger("kafka")


class KafkaBroker:
    def __init__(self, bootstrap: str | None = None, partitions: int | None = None, replication: int = 1):
        s = get_settings()
        self.bootstrap = bootstrap or s.kafka_bootstrap
        self.partitions = partitions or s.topic_partitions
        self.replication = replication
        self.security = {"security.protocol": s.kafka_security_protocol}
        self._producer = Producer(
            {
                **self.security,
                "bootstrap.servers": self.bootstrap,
                "enable.idempotence": True,
                "compression.type": "lz4",
                "linger.ms": 20,
                "batch.num.messages": 10000,
                "queue.buffering.max.messages": 500000,
                "acks": "all",
            }
        )

    def ensure_topics(self) -> None:
        admin = AdminClient({"bootstrap.servers": self.bootstrap, **self.security})
        futures = admin.create_topics([NewTopic(t, self.partitions, self.replication) for t in ALL_TOPICS])
        for topic, fut in futures.items():
            try:
                fut.result()
            except KafkaException as e:
                if "TOPIC_ALREADY_EXISTS" not in str(e):
                    raise
                logger.debug("topic %s already exists", topic)

    def healthy(self) -> bool:
        try:
            self._producer.list_topics(timeout=2)
            return True
        except KafkaException:
            return False

    def produce(self, topic: str, key: str | None, value: bytes, headers: dict[str, str] | None = None) -> None:
        while True:
            try:
                self._producer.produce(topic, value=value, key=key, headers=list((headers or {}).items()) or None)
                self._producer.poll(0)
                return
            except BufferError:  # local queue full -> back-pressure the caller
                self._producer.poll(0.05)

    def flush(self, timeout_s: float = 10.0) -> int:
        return self._producer.flush(timeout_s)

    def subscribe(self, group: str, topics: list[str]) -> KafkaConsumerAdapter:
        return KafkaConsumerAdapter(self.bootstrap, group, topics, self.security)


class KafkaConsumerAdapter:
    def __init__(self, bootstrap: str, group: str, topics: list[str], security: dict[str, str] | None = None):
        self.topics = topics
        self._c = KConsumer(
            {
                **(security or {}),
                "bootstrap.servers": bootstrap,
                "group.id": group,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
                "partition.assignment.strategy": "cooperative-sticky",
                "max.poll.interval.ms": 300000,
            }
        )
        self._c.subscribe(topics)
        self._paused: list[TopicPartition] = []

    def poll_batch(self, max_messages: int, timeout_s: float) -> list[Message]:
        out: list[Message] = []
        for m in self._c.consume(num_messages=max_messages, timeout=timeout_s):
            if m.error():
                logger.warning("consumer error: %s", m.error())
                continue
            key, value = m.key(), m.value()
            headers = {str(h[0]): (h[1].decode() if isinstance(h[1], bytes) else str(h[1])) for h in (m.headers() or [])}
            out.append(Message(m.topic() or "", key.decode() if isinstance(key, bytes) else key, value or b"", m.partition() or 0,
                               m.offset() or 0, headers))
        return out

    def commit(self, messages: list[Message]) -> None:
        latest: dict[tuple[str, int], int] = {}
        for m in messages:
            k = (m.topic, m.partition)
            latest[k] = max(latest.get(k, -1), m.offset)
        if latest:
            self._c.commit(offsets=[TopicPartition(t, p, o + 1) for (t, p), o in latest.items()], asynchronous=False)

    def pause(self) -> None:
        self._paused = self._c.assignment()
        self._c.pause(self._paused)

    def resume(self) -> None:
        if self._paused:
            self._c.resume(self._paused)
            self._paused = []

    def lag(self) -> dict[str, int]:
        out: dict[str, int] = {t: 0 for t in self.topics}
        for tp in self._c.assignment():
            low, high = self._c.get_watermark_offsets(tp, timeout=2)
            pos = self._c.position([tp])[0].offset
            out[tp.topic] = out.get(tp.topic, 0) + max(0, high - (pos if pos >= 0 else low))
        return out

    def close(self) -> None:
        self._c.close()
