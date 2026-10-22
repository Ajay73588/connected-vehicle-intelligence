"""Stream processor service: consumer loop with retry, back-pressure, DLQ bisection, metrics and health."""

from __future__ import annotations

import logging
import signal
import threading
import time

from prometheus_client import start_http_server

from cvi.common import metrics
from cvi.common.config import get_settings
from cvi.common.logging import setup_logging
from cvi.common.resilience import backoff_delays
from cvi.messaging.base import TOPIC_VALIDATED, Broker, Message
from cvi.messaging.factory import get_broker
from cvi.stream.processor import Processor, SinkUnavailable

logger = logging.getLogger("stream.worker")


class Worker:
    def __init__(self, broker: Broker, processor: Processor, group: str = "stream-processor"):
        self.s = get_settings()
        self.broker, self.p = broker, processor
        self.consumer = broker.subscribe(group, [TOPIC_VALIDATED])
        self.running = threading.Event()
        self.running.set()
        self.ready = False

    def stop(self, *_):
        self.running.clear()

    def _process_with_retry(self, batch: list[Message]) -> None:
        """Infrastructure failures -> pause the consumer and retry the SAME batch with exponential backoff (back-pressure).
        Poison messages -> bisect the batch down to single messages and dead-letter the offenders."""
        attempt = 0
        while self.running.is_set():
            try:
                self.p.process_batch(batch)
                return
            except SinkUnavailable as e:
                attempt += 1
                metrics.backpressure_pauses_total.inc()
                self.consumer.pause()
                delay = min(10.0, 0.2 * 2**attempt)
                logger.warning("sink unavailable (%s); attempt %d, retrying in %.1fs", e, attempt, delay)
                time.sleep(delay)
            except Exception as e:  # noqa: BLE001 - poison message path
                logger.error("batch failed with %r; bisecting", e)
                self._bisect(batch)
                return
            finally:
                self.consumer.resume()

    def _bisect(self, batch: list[Message]) -> None:
        if len(batch) == 1:
            self.p._dlq(batch[0], "processing_error", "poison message")
            return
        mid = len(batch) // 2
        for half in (batch[:mid], batch[mid:]):
            try:
                self.p.process_batch(half)
            except SinkUnavailable:
                self._process_with_retry(half)
            except Exception:  # noqa: BLE001
                self._bisect(half)

    def run(self) -> None:
        last_lag = 0.0
        self.ready = True
        while self.running.is_set():
            try:
                batch = self.consumer.poll_batch(self.s.batch_size, self.s.batch_timeout_s)
            except Exception as e:  # noqa: BLE001 - broker unreachable: stay alive, retry with a pause
                logger.error("poll failed, retrying: %s", e)
                time.sleep(0.5)
                continue
            if batch:
                self._process_with_retry(batch)
                if self.running.is_set():
                    try:
                        self.consumer.commit(batch)
                    except Exception as e:  # noqa: BLE001 - offsets stay uncommitted -> batch is re-delivered, dedupe makes that safe
                        logger.error("offset commit failed (batch will be re-delivered): %s", e)
            if time.monotonic() - last_lag > 5:
                last_lag = time.monotonic()
                try:
                    for topic, lag in self.consumer.lag().items():
                        metrics.consumer_lag.labels(topic).set(lag)
                    metrics.vehicles_active.set(sum(self.p.state.active_count(t) for t in ("acme-logistics", "borealis-ev", "metro-transit")))
                except Exception:  # noqa: BLE001
                    pass
        self.p.flush()
        self.consumer.close()


def main() -> None:
    from cvi.storage.mongo_store import get_raw_store
    from cvi.storage.object_store import get_object_store
    from cvi.storage.pg import get_pool
    from cvi.storage.redis_store import StateStore
    from cvi.stream.pg_sink import PgSink

    s = get_settings()
    setup_logging("stream", s.log_level)
    start_http_server(s.metrics_port_stream)
    broker = get_broker()
    for attempt in range(30):  # wait for dependencies (compose start order is not a readiness guarantee)
        try:
            broker.ensure_topics()
            break
        except Exception as e:  # noqa: BLE001
            logger.warning("waiting for broker: %s", e)
            time.sleep(min(5, backoff_delays(attempt + 1, 0.5, 5, False)[-1]))
    raw = get_raw_store()
    raw.ensure_indexes()
    store = get_object_store()
    if hasattr(store, "ensure_bucket"):
        store.ensure_bucket()
    pool = get_pool(s.worker_database_url or s.database_url)
    proc = Processor(broker, StateStore(), raw, store, PgSink(pool))
    w = Worker(broker, proc)
    signal.signal(signal.SIGTERM, w.stop)
    signal.signal(signal.SIGINT, w.stop)
    logger.info("stream processor started")
    w.run()


if __name__ == "__main__":
    main()
