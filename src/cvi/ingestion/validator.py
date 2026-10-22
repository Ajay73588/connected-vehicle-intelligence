"""Validation stage: telemetry.raw -> (schema/VIN/OBD checks) -> telemetry.validated | telemetry.dlq.

Idempotency note: this stage is stateless and deterministic, so re-processing after a crash only re-emits identical
messages; the stream processor de-duplicates by event_id downstream."""

from __future__ import annotations

import logging
import signal
import threading
import time

import orjson
from prometheus_client import start_http_server
from pydantic import ValidationError

from cvi.algorithms.obd import parse_mode1_response
from cvi.common import metrics
from cvi.common.config import get_settings
from cvi.common.logging import setup_logging
from cvi.messaging.base import TOPIC_DLQ, TOPIC_RAW, TOPIC_VALIDATED, Broker, Message
from cvi.messaging.factory import get_broker
from cvi.schemas.telemetry import MAX_FUTURE_SKEW_MS, TelemetryEvent

logger = logging.getLogger("validator")


def validate_payload(raw: bytes, now_ms: int | None = None) -> tuple[dict | None, str, str]:
    """Return (event_dict, "", "") on success or (None, reason, detail)."""
    now_ms = now_ms or int(time.time() * 1000)
    try:
        d = orjson.loads(raw)
        if not isinstance(d, dict):
            return None, "malformed", "payload is not a JSON object"
    except orjson.JSONDecodeError as e:
        return None, "malformed", str(e)
    obd = d.get("obd_raw")
    if obd:  # fill signals from a raw OBD-II mode-01 frame when the device did not send them decoded
        try:
            for k, v in parse_mode1_response(obd).items():
                d.setdefault(k, v)
        except ValueError as e:
            return None, "obd_parse_error", str(e)
    try:
        ev = TelemetryEvent.model_validate(d)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(map(str, first["loc"]))
        reason = "invalid_vin" if loc == "vin" else "schema_violation"
        return None, reason, f"{loc}: {first['msg']}"
    if ev.ts > now_ms + MAX_FUTURE_SKEW_MS:
        return None, "timestamp_in_future", f"ts {ev.ts} is > {MAX_FUTURE_SKEW_MS // 60000} min ahead of server time"
    return ev.model_dump(), "", ""


class Validator:
    def __init__(self, broker: Broker, group: str = "validator"):
        self.s = get_settings()
        self.broker = broker
        self.consumer = broker.subscribe(group, [TOPIC_RAW])
        self.running = threading.Event()
        self.running.set()

    def handle(self, batch: list[Message]) -> tuple[int, int]:
        ok = bad = 0
        for m in batch:
            ev, reason, detail = validate_payload(m.value)
            if ev is None:
                bad += 1
                metrics.dlq_total.labels(reason).inc()
                metrics.events_failed_total.labels("validator", reason).inc()
                self.broker.produce(TOPIC_DLQ, m.key, orjson.dumps({"reason": reason, "detail": detail[:300],
                                    "value": m.value[:2000].decode("utf-8", "replace")}), {"reason": reason})
            else:
                ok += 1
                headers = {"ingest_ts": m.headers.get("ingest_ts", str(int(time.time() * 1000)))}
                self.broker.produce(TOPIC_VALIDATED, ev["vin"], orjson.dumps(ev), headers)
        metrics.events_processed_total.labels("validator").inc(ok)
        return ok, bad

    def run(self) -> None:
        while self.running.is_set():
            try:
                batch = self.consumer.poll_batch(self.s.batch_size * 4, 0.5)
            except Exception as e:  # noqa: BLE001 - broker unreachable: keep the loop alive and retry
                logger.error("poll failed, retrying: %s", e)
                time.sleep(0.5)
                continue
            if not batch:
                time.sleep(0.01)
                continue
            try:
                self.handle(batch)
                self.broker.flush(5)  # outputs durable BEFORE the input offsets are committed
                self.consumer.commit(batch)
            except Exception as e:  # noqa: BLE001 - broker blip: do not commit, batch is re-polled after restart
                logger.error("validator batch failed, backing off: %s", e)
                time.sleep(1)
        self.consumer.close()


def main() -> None:
    s = get_settings()
    setup_logging("validator", s.log_level)
    start_http_server(s.metrics_port_stream + 1)
    broker = get_broker()
    for _ in range(30):
        try:
            broker.ensure_topics()
            break
        except Exception as e:  # noqa: BLE001
            logger.warning("waiting for broker: %s", e)
            time.sleep(2)
    v = Validator(broker)
    signal.signal(signal.SIGTERM, lambda *_: v.running.clear())
    signal.signal(signal.SIGINT, lambda *_: v.running.clear())
    v.run()


if __name__ == "__main__":
    main()
