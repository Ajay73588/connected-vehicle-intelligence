"""Where simulated messages go. Each sink takes a list of (key, payload) wire messages."""

from __future__ import annotations

import sys
import time
from typing import Protocol

Wire = list[tuple[str | None, bytes]]


class Sink(Protocol):
    def send(self, msgs: Wire) -> None: ...
    def close(self) -> None: ...


class MemorySink:
    def __init__(self):
        self.msgs: Wire = []

    def send(self, msgs: Wire) -> None:
        self.msgs.extend(msgs)

    def close(self) -> None: ...


class NullSink(MemorySink):
    """Counts but discards (measures pure generation throughput)."""

    def __init__(self):
        super().__init__()
        self.count = 0

    def send(self, msgs: Wire) -> None:
        self.count += len(msgs)


class StdoutSink:
    def send(self, msgs: Wire) -> None:
        for _, v in msgs:
            sys.stdout.buffer.write(v + b"\n")
        sys.stdout.buffer.flush()

    def close(self) -> None: ...


class KafkaSink:
    """Publishes straight into telemetry.raw (what a managed IoT bridge would do)."""

    def __init__(self):
        from cvi.messaging.base import TOPIC_RAW
        from cvi.messaging.factory import get_broker

        self.topic = TOPIC_RAW
        self.broker = get_broker()
        self.broker.ensure_topics()

    def send(self, msgs: Wire) -> None:
        h = {"ingest_ts": str(int(time.time() * 1000))}
        for k, v in msgs:
            self.broker.produce(self.topic, k, v, h)

    def close(self) -> None:
        self.broker.flush(10)


class HttpSink:
    def __init__(self, url: str, key: str, batch: int = 1000):
        import httpx

        self.c = httpx.Client(timeout=10, headers={"X-Device-Key": key, "Content-Type": "application/json"})
        self.url, self.batch = url, batch

    def send(self, msgs: Wire) -> None:
        for i in range(0, len(msgs), self.batch):
            body = b"[" + b",".join(v for _, v in msgs[i : i + self.batch]) + b"]"
            for attempt in range(5):
                r = self.c.post(self.url, content=body)
                if r.status_code == 202:
                    break
                time.sleep(min(2.0, 0.2 * 2**attempt))  # honour back-pressure (503 Retry-After)

    def close(self) -> None:
        self.c.close()


class MqttSink:
    def __init__(self, host: str, port: int):
        import paho.mqtt.client as mqtt

        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{int(time.time())}")
        self.c.connect(host, port)
        self.c.loop_start()

    def send(self, msgs: Wire) -> None:
        import orjson

        for k, v in msgs:
            try:
                tenant = orjson.loads(v).get("tenant", "unknown")
            except Exception:  # noqa: BLE001 - malformed payloads are sent as-is on purpose
                tenant = "unknown"
            self.c.publish(f"telemetry/{tenant}/{k or 'unknown'}", v, qos=1)

    def close(self) -> None:
        self.c.loop_stop()
        self.c.disconnect()


def make_sink(name: str, **kw) -> Sink:
    from cvi.common.config import get_settings

    s = get_settings()
    if name == "kafka":
        return KafkaSink()
    if name == "http":
        return HttpSink(kw.get("url") or f"http://localhost:{s.ingestion_port}/v1/telemetry", s.ingest_api_key)
    if name == "mqtt":
        return MqttSink(s.mqtt_host, s.mqtt_port)
    if name == "stdout":
        return StdoutSink()
    if name == "null":
        return NullSink()
    raise ValueError(f"unknown sink {name}")
