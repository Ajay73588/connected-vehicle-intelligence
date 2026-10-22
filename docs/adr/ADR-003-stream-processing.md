# ADR-003: Stream processing architecture — a stateful micro-batch consumer with idempotent effects (not Flink/Spark)

- Status: accepted
- Date: 2026-10-01

## Context
Alerts must be raised < 5 s after the offending event, aggregates must be exact despite duplicates, out-of-order and late events,
and the whole thing must run on a laptop and scale out in Kubernetes.

## Decision
A Python service (`cvi.stream.worker` + `cvi.stream.processor`) that consumes `telemetry.validated` in micro-batches (default 500
messages / 0.5 s) and applies, per batch, in this exact order:

1. decode, envelope check → poison/malformed → DLQ
2. vehicle + tenant resolution (cached from PostgreSQL); a VIN claiming a tenant it does not belong to → DLQ `tenant_mismatch`
3. raw sinks: MongoDB upsert by `event_id` + Parquet buffer (**idempotent**)
4. read-only duplicate check (Redis seen-set; in-process Bloom filter only when Redis is down)
5. **event-time ordering and detectors** per vehicle (sliding windows keyed by event time, EWMA z-scores, rules)
6. persist alerts (`UNIQUE dedup_key`) + health/safety snapshots; publish to `alerts`
7. latest state + geohash map cells in Redis
8. **atomic commit** of "seen" marks and aggregates (one Lua script: `SADD` is the idempotency gate, the same call increments the aggregates)
9. commit Kafka offsets

**Delivery semantics:** at-least-once delivery, *effectively-once effects*. A crash at any step is repaired by replaying the
uncommitted batch: steps 3, 6, 7 are idempotent, steps 4/8 make aggregates exactly-once, and the per-vehicle in-memory result
cache re-emits the identical alerts when a batch is retried inside the same process.

**Ordering / time:** event time (`ts`, device clock) is authoritative. Per vehicle the processor keeps `last_ts`. An event
older than `last_ts` but within `ALLOWED_LATENESS_S` (300 s) is *out of order*: it updates windows (inserted in sorted position) and
aggregates but never overwrites the latest-state. Older than that it is *late*: stored raw, counted, excluded from windows and
aggregates. Within a batch, events of a vehicle are sorted by `(ts, seq)` first. Sequence gaps are counted per vehicle.

**Back-pressure and failure handling:** sink failure → `SinkUnavailable` → the worker pauses the consumer, retries the *same* batch
with exponential back-off and does not commit; circuit breakers (redis / mongo / postgres / object store) stop hammering a dead
dependency. Redis down → degrade (Bloom-filter dedupe, aggregates skipped, state not updated); object store down → buffer and retry,
back-pressure only if the backlog exceeds 4 flush-sizes. Poison message → bisect the batch until the offender is isolated, send it to
the DLQ, process the rest.

## Alternatives considered
* **Apache Flink / Kafka Streams**: exactly-once with checkpointed state and rich windowing. Heavier to operate and test on a laptop,
  JVM skill set; for rule-based per-vehicle detectors the state is small (a few windows per vehicle) and our idempotent-sink design
  gives the same effect. Flink is the natural next step if stateful joins across vehicles are required.
* **Spark Structured Streaming**: micro-batch seconds of latency; poor fit for the < 5 s alert target.
* **Per-event processing** (no batches): simpler, but 10-50x more Redis/PostgreSQL round-trips; batching is what makes the Lua gate
  and bulk upserts cheap.
* **Kafka transactions (EOS)**: only covers Kafka→Kafka; our sinks are Redis/Postgres/Mongo/S3, so idempotent writes are required anyway.

## Trade-offs / consequences
* Python throughput per process is limited (measured numbers in `docs/performance/benchmarks.md`); scale is horizontal
  (consumer-group members = partitions). A Go/Rust rewrite of the hot loop is the escape hatch if the per-core cost dominates.
* Detector windows live in process memory and are lost on restart (they re-warm in ≤ 30 min of event time; alerts that depend on a
  full 30-minute driver window may be delayed). Persisting windows to Redis was rejected for cost/complexity at 100K vehicles.
* Known residual risk: a crash after step 8 but before offset commit replays events that the Redis gate marks as seen — their side
  effects were already applied, so nothing is lost; a crash between steps 6 and 8 re-runs detection on replay and relies on
  `dedup_key` to avoid duplicate alerts.
