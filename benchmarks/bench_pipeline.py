#!/usr/bin/env python
"""In-process pipeline benchmark: simulator -> chaos -> validator -> stream processor (real Redis, real PostgreSQL sink).

Measures what THIS machine can do for one worker process, stage by stage, with the real code paths:
  generate   FleetEngine.step + ChaosInjector.apply (+ JSON serialisation)
  validate   Validator.handle (pydantic schema, VIN check digit, OBD parsing, routing)
  process    Processor.process_batch (dedupe, ordering, detectors, alerts, Redis state + atomic aggregates, Mongo-equivalent
             raw store, Parquet writer, PostgreSQL alert/snapshot sink)
Not measured here (no Kafka/MongoDB on the dev machine): broker I/O, MongoDB write latency. See docs/performance/benchmarks.md.

  python benchmarks/bench_pipeline.py --vehicles 20000 --events 60000 --redis redis://localhost:6379/3 --sink pg|memory
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import psutil  # noqa: E402


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicles", type=int, default=20_000)
    ap.add_argument("--events", type=int, default=60_000)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--redis", default="redis://localhost:6379/3")
    ap.add_argument("--sink", choices=["pg", "memory"], default="pg")
    ap.add_argument("--no-chaos", action="store_true")
    ap.add_argument("--profile", action="store_true", help="cProfile the processing stage and print the top functions")
    ap.add_argument("--out", default=str(ROOT / "benchmarks" / "results" / "pipeline.json"))
    a = ap.parse_args()

    os.environ.update({"BROKER": "memory", "REDIS_URL": a.redis, "MONGO_URL": "memory://", "OBJECT_STORE": "local",
                       "LOCAL_OBJECT_DIR": tempfile.mkdtemp(prefix="bench-obj-")})
    from cvi.common.config import get_settings

    s = get_settings()
    from cvi.ingestion.validator import Validator
    from cvi.messaging.base import TOPIC_RAW, TOPIC_VALIDATED
    from cvi.messaging.memory import MemoryBroker
    from cvi.simulator.chaos import ChaosInjector
    from cvi.simulator.engine import ChaosConfig, FleetEngine
    from cvi.simulator.scenarios import RESERVED
    from cvi.storage.mongo_store import MemoryRawStore
    from cvi.storage.object_store import LocalObjectStore
    from cvi.storage.redis_store import StateStore, get_redis
    from cvi.stream.processor import Processor

    r = get_redis(a.redis)
    r.flushdb()
    proc_info = psutil.Process()
    broker = MemoryBroker(12)
    broker.ensure_topics()
    result: dict = {"machine": {"cpu": platform.processor(), "logical_cores": os.cpu_count(), "python": platform.python_version(),
                                "ram_gb": round(psutil.virtual_memory().total / 2**30, 1), "os": platform.platform()},
                    "params": vars(a)}

    # ---- stage 1: generation
    eng = FleetEngine(a.vehicles, reserved=RESERVED)
    inj = ChaosInjector(ChaosConfig(0, 0, 0, 0) if a.no_chaos else ChaosConfig())
    t0 = time.perf_counter()
    n_wire = 0
    now = int(time.time() * 1000)
    while n_wire < a.events:
        wire = inj.apply(eng.step(min(5000, a.events - n_wire), now))
        for k, v in wire:
            broker.produce(TOPIC_RAW, k, v, {"ingest_ts": str(now)})
        n_wire += len(wire)
        now += 1000
    gen_s = time.perf_counter() - t0
    result["generate"] = {"messages": n_wire, "seconds": round(gen_s, 2), "msgs_per_s": int(n_wire / gen_s), "chaos": inj.stats}

    # ---- stage 2: validation
    v = Validator(broker, "bench-validator")
    t0 = time.perf_counter()
    ok = bad = 0
    lat = []
    while True:
        batch = v.consumer.poll_batch(a.batch * 4, 0)
        if not batch:
            break
        b0 = time.perf_counter()
        o, b_ = v.handle(batch)
        lat.append((time.perf_counter() - b0) / len(batch))
        ok += o
        bad += b_
    val_s = time.perf_counter() - t0
    result["validate"] = {"valid": ok, "rejected_to_dlq": bad, "seconds": round(val_s, 2), "msgs_per_s": int((ok + bad) / val_s)}

    # ---- stage 3: stream processing
    objects = LocalObjectStore(os.environ["LOCAL_OBJECT_DIR"])
    if a.sink == "pg":
        from cvi.storage.pg import get_pool
        from cvi.stream.pg_sink import PgSink

        sink = PgSink(get_pool(s.worker_database_url or s.database_url))
    else:
        from helpers import MemorySink

        with __import__("psycopg").connect(s.admin_database_url) as c:
            rows = c.execute("SELECT v.vin, v.id, v.tenant_id, t.slug FROM vehicles v JOIN tenants t ON t.id = v.tenant_id WHERE v.id <= %s", [a.vehicles]).fetchall()
        sink = MemorySink({r_[0].strip(): (r_[1], r_[2], r_[3]) for r_ in rows})
    proc = Processor(broker, StateStore(r), MemoryRawStore(max_per_vin=50), objects, sink)
    cons = broker.subscribe("bench-stream", [TOPIC_VALIDATED])
    batch_lat, total = [], 0
    cpu0 = proc_info.cpu_times()
    t0 = time.perf_counter()
    totals = {"duplicates": 0, "late": 0, "out_of_order": 0, "dlq": 0, "alerts": 0, "processed": 0}
    prof = None
    if a.profile:
        import cProfile

        prof = cProfile.Profile()
        prof.enable()
    while True:
        msgs = cons.poll_batch(a.batch, 0)
        if not msgs:
            break
        b0 = time.perf_counter()
        res = proc.process_batch(msgs)
        batch_lat.append(time.perf_counter() - b0)
        total += len(msgs)
        for k in totals:
            totals[k] += getattr(res, k)
        cons.commit(msgs)
    proc.flush()
    if prof:
        import pstats

        prof.disable()
        pstats.Stats(prof).sort_stats("tottime").print_stats(18)
    proc_s = time.perf_counter() - t0
    cpu1 = proc_info.cpu_times()
    result["process"] = {"messages": total, "seconds": round(proc_s, 2), "events_per_s": int(total / proc_s), "batch_size": a.batch,
                         "batch_latency_ms": {"p50": round(pct(batch_lat, .5) * 1000, 1), "p95": round(pct(batch_lat, .95) * 1000, 1),
                                              "p99": round(pct(batch_lat, .99) * 1000, 1)},
                         "per_event_us": round(proc_s / total * 1e6, 1), "cpu_seconds": round((cpu1.user + cpu1.system) - (cpu0.user + cpu0.system), 2),
                         "rss_mb": round(proc_info.memory_info().rss / 2**20), "sink": a.sink, **totals}
    # correctness cross-check against the aggregates in Redis (exactly-once effect even with chaos duplicates)
    agg_n = sum(float(r.hget(k, "n") or 0) for k in r.scan_iter(match="agg:*", count=1000))
    result["process"]["aggregate_events_in_redis"] = int(agg_n)
    result["process"]["distinct_events_expected"] = totals["processed"] - totals["late"]  # late events are counted separately, not in n
    print(json.dumps(result, indent=2))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(result, indent=2))
    r.flushdb()
    from cvi.storage.pg import close_pools

    close_pools()


if __name__ == "__main__":
    main()
