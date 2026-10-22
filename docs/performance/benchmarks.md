# Performance & benchmark evidence

Every number in this file is produced by the scripts in `benchmarks/` and stored as JSON in `benchmarks/results/`; this page is rendered by
`benchmarks/make_report.py`. Nothing is estimated unless it is explicitly labelled **theoretical**.

## 0. What was and was not measured

* Machine: AMD64 Family 25 Model 68 Stepping 1, AuthenticAMD, 16 logical cores, 15.8 GB RAM, Windows-11-10.0.26200-SP0, Python 3.13.7. A *laptop*, with the load generator, PostgreSQL, Redis and the service under test all on the same box.
* **Measured here (local benchmark):** simulator generation, validator, stream-processor core (real Redis server, real PostgreSQL, Parquet writer), API latency (real PostgreSQL with the 100,000-vehicle / 1,000,000-alert dataset, real Redis).
* **Not measured here:** Kafka broker I/O and consumer lag (no Docker/Kafka on this machine; an in-memory broker with Kafka semantics was used), MongoDB write latency (an in-memory raw-event store was used), multi-node behaviour, any cloud environment. The Redis server used is the old 3.0.504 Windows build (no Redis 7 available); newer Redis is generally faster.
* **Not achieved:** a sustained **100,000 events/s** run. That number is reached only by scaling out (section 3, **theoretical**).

## 1. Simulator throughput (100,000 vehicles, chaos enabled)

| worker processes | messages/s generated |
|---|---|
| 1 | 79,528 |
| 2 | 157,702 |
| 4 | 213,306 |

Includes duplicates, out-of-order, late and malformed injection and JSON serialisation (`benchmarks/bench_simulator.py`). One process already exceeds 100K msg/s of *generation* on this CPU; the point of `--workers` is to leave headroom for the sink (Kafka producer).

## 2. Pipeline stages, single process (`benchmarks/bench_pipeline.py`)

Input: 120,076 wire messages for 20,000 vehicles with injected chaos (duplicates 1,231, out-of-order 2,397, late 341, malformed 339).

| stage | throughput | notes |
|---|---|---|
| generate + chaos | 91,717 msg/s | |
| validate (schema, VIN check digit, OBD, routing) | 32,261 msg/s | 339 rejected to DLQ |
| stream processing, PostgreSQL sink | **4,452 events/s** (224.6 µs/event) | batch of 500: p50 80.7 ms, p95 201.9 ms, p99 692.4 ms; CPU 23.52 s for 26.89 s wall; RSS 2948 MB |
| stream processing, in-memory sink (isolates PostgreSQL cost) | 6,600 events/s (151.5 µs/event) | batch p50 59.1 ms, p95 76.1 ms, p99 521.7 ms |

**Correctness check inside the benchmark:** after processing, the sum of the `n` aggregate across Redis hourly counters is 118,228 and the number of distinct non-late events is 118,228 — equal, i.e. every event was counted exactly once although 1,231 duplicates were delivered; 278 late events were counted separately, 123 alerts were created, 0 poison messages.

Where the time goes (cProfile, `--profile`): client-side Redis command serialisation and the Lua-gate round trips dominate, then PostgreSQL pipeline I/O and the detectors (~50 µs/event). Two optimisations came directly from profiling: latest state is stored as one JSON value instead of a ~50-field hash (2.9K → 5.0K events/s) and PostgreSQL snapshots are bulk-upserted with `executemany` (pipeline mode).

## 3. Capacity model for 100,000 events/s — **theoretical, extrapolated from the measurements above**

* Stream processors are partition-parallel and share nothing except Redis/PostgreSQL/MongoDB. With 24 partitions the maximum useful replicas is 24. At the measured 4,452 events/s/replica (PostgreSQL sink) that is ≈ 106,848 events/s (≥ the 100,000 target); at 6,600 (sink cost removed) ≈ 158,400. Required replicas at 100,000/s: 23 (PostgreSQL sink as measured). PostgreSQL write amplification is controlled by `SNAPSHOT_INTERVAL_S` (default 15 s per vehicle) — raise partitions to 48 for more headroom.
* Validators: 32,261 msg/s per replica ⇒ 4 replicas for 100K/s. Edge: stateless FastAPI appenders (not benchmarked in isolation); plan ≥ 8 replicas behind the load balancer for 100 batches/s of 1,000 events.
* **3× burst for 5 minutes** (300K events/s) — Kafka absorbs what the consumers cannot process; backlog = (300K − capacity) × 300 s and the drain time afterwards = backlog / (capacity − 100K):

| stream-processor replicas (= partitions) | capacity (theoretical) | backlog after a 5-min 3× burst | time to drain at 100K/s steady load |
|---|---|---|---|
| 24 | 106,848 ev/s | 57,945,600 events | 141 min |
| 48 | 213,696 ev/s | 25,891,200 events | 4 min |
| 96 | 427,392 ev/s | 0 events | 0 min |

  Reading: 24 partitions barely reach the steady-state target on this hardware class, so recovering from a burst needs headroom — provision 48 partitions/replicas (or faster cores / a faster language for the hot loop). Kafka retention (24 h) comfortably holds the backlog (~58M events ≈ 20–25 GB at ~400 B/event for the 24-replica case). KEDA scales on lag up to the partition ceiling.
* Kafka, MongoDB and network were **not** in the loop for these numbers; real throughput will be lower per replica if those add latency. A cloud benchmark with `tests/load/k6_ingest.js` + the 100K simulator is the required confirmation and has **not** been run.

## 4. API latency (`benchmarks/bench_api.py`)

16 concurrent keep-alive clients, one uvicorn worker process, 100,000-vehicle dataset (tenant of 45,000 vehicles, 1M alerts), real PostgreSQL (RLS on) and Redis; client-side wall time in ms, successful requests only.

| endpoint | requests | rps | p50 | p95 | p99 | status codes |
|---|---|---|---|---|---|---|
| `GET /vehicles?limit=50` | 2,019 | 200.8 | 71.4 | 89.4 | 104.9 | {'200': 2019} |
| `GET /vehicles (keyset page 2)` | 1,907 | 189.8 | 74.7 | 90.9 | 98.4 | {'200': 1907} |
| `GET /vehicles/{id}` | 3,227 | 322.0 | 43.0 | 54.7 | 63.8 | {'200': 3227} |
| `GET /alerts?status=open` | 2,223 | 221.5 | 63.0 | 80.0 | 94.8 | {'200': 2223} |
| `GET /analytics/summary` | 5,700 | 569.2 | 23.4 | 29.3 | 119.1 | {'200': 5700} |
| `GET /analytics/maintenance` | 3,143 | 313.8 | 44.0 | 53.3 | 78.0 | {'200': 3143} |
| `GET /map/vehicles` | 2,286 | 228.1 | 62.5 | 71.7 | 77.2 | {'200': 2286} |

Targets (API p95 < 200 ms, p99 < 500 ms) were met for every endpoint at this concurrency on this machine. Read endpoints that aggregate (`/analytics/*`) are cached in Redis for 2–30 s with a stampede lock; the executive summary is the 2-second case. Dashboard refresh latency is therefore dominated by the API round trip (tens of ms), well under the 2 s target. Single-process Python is the limit: run replicas for higher throughput (HPA on CPU).

### Alert latency (event → persisted alert)
`scripts/e2e.py` measures it end to end in the embedded stack: all four scenarios produced their alert **0.8–1.8 s** after injection (including 0.5 s polling granularity), within the 5 s critical-alert target. In production the same latency is exposed as the `alert_latency_seconds` histogram (SLO alert at p99 > 5 s).

## 5. SQL evidence
See `sql-before-after.md` (real `EXPLAIN (ANALYZE, BUFFERS)` before/after).

## 6. Reproduce
```bash
python benchmarks/bench_simulator.py
python benchmarks/bench_pipeline.py --events 120000 --sink pg      --out benchmarks/results/pipeline_pg_sink.json
python benchmarks/bench_pipeline.py --events 120000 --sink memory  --out benchmarks/results/pipeline_memory_sink.json
python benchmarks/bench_api.py --base http://127.0.0.1:8000        # RATE_LIMIT_PER_MINUTE must be raised for load tests
python benchmarks/make_report.py
```
