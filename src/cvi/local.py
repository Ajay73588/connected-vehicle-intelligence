"""Embedded single-process dev mode: API + validator + stream processor (+ optional simulator) in one process.

Uses the in-memory broker, fakeredis and an in-memory raw-event store, so the only external dependency is PostgreSQL.
  python -m cvi.local --simulate 1000 --eps 25
This exists so the whole pipeline can be exercised without Docker/Kafka; production uses `docker compose up`
(real Kafka, Redis, MongoDB, MinIO).  Test doubles are NOT used by any deployed service."""

from __future__ import annotations

import argparse
import os
import threading
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", type=int, default=0, help="vehicles to simulate (0 = none)")
    ap.add_argument("--eps", type=float, default=25)
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    os.environ.setdefault("BROKER", "memory")
    os.environ.setdefault("REDIS_URL", "fakeredis://")
    os.environ.setdefault("MONGO_URL", "memory://")
    os.environ.setdefault("OBJECT_STORE", "local")

    import uvicorn

    from cvi.api.context import get_ctx
    from cvi.common.config import get_settings
    from cvi.ingestion.validator import Validator
    from cvi.storage.pg import get_pool
    from cvi.stream.pg_sink import PgSink
    from cvi.stream.processor import Processor
    from cvi.stream.worker import Worker

    s = get_settings()
    ctx = get_ctx()
    ctx.broker.ensure_topics()
    ctx.raw.ensure_indexes()
    validator = Validator(ctx.broker)
    proc = Processor(ctx.broker, ctx.state, ctx.raw, ctx.objects, PgSink(get_pool(s.worker_database_url or s.database_url)))
    worker = Worker(ctx.broker, proc)
    threading.Thread(target=validator.run, daemon=True, name="validator").start()
    threading.Thread(target=worker.run, daemon=True, name="stream").start()

    if a.simulate:
        from cvi.simulator.chaos import ChaosInjector
        from cvi.simulator.engine import ChaosConfig, FleetEngine
        from cvi.simulator.scenarios import RESERVED
        from cvi.simulator.sinks import KafkaSink

        def sim():
            eng = FleetEngine(a.simulate, reserved=RESERVED)
            inj = ChaosInjector(ChaosConfig(), allowed_lateness_s=s.allowed_lateness_s)
            sink = KafkaSink()  # works with the memory broker too (same Broker interface)
            while True:
                t0 = time.time()
                sink.send(inj.apply(eng.step(max(1, int(a.eps * 0.2)), int(time.time() * 1000))))
                time.sleep(max(0.0, 0.2 - (time.time() - t0)))

        threading.Thread(target=sim, daemon=True, name="simulator").start()

    uvicorn.run("cvi.api.app:create_app", factory=True, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
