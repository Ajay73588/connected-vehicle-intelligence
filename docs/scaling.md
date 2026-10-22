# Scalability and capacity

Measured numbers live in `docs/performance/benchmarks.md` (generated); this page explains *how the system scales* and what limits it.

## Horizontal scaling by component
| Component | Scaling unit | Limit | Autoscaling signal |
|---|---|---|---|
| ingestion-edge | stateless replica | Kafka producer throughput, LB | CPU (HPA 55%) |
| Kafka | partitions + brokers (VIN key) | 48 partitions (cloud profile) | — (capacity planned) |
| validator | consumer-group member | partitions | CPU (HPA 60%) |
| stream-processor | consumer-group member (owns whole vehicles) | partitions (48) | **Kafka consumer lag** (KEDA), CPU fallback |
| API / agent | stateless replica | PostgreSQL connections (pool 10/replica), Redis | CPU (HPA 60%) |
| PostgreSQL | vertical + read replica; time-partition `alerts`/`audit_log` beyond ~100M rows | write rate of snapshots (throttled by `SNAPSHOT_INTERVAL_S`) | CloudWatch alarms |
| Redis | node size; Redis Cluster with `{vin}`/`{tenant}` hash tags if one node is not enough | memory (3 h seen-sets dominate) | memory alarm |
| MongoDB | shards by `{tenant, hashed vin}` | write IOPS | — |
| object store | unlimited | request rate per prefix (tenant/date/hour spreads keys) | — |

## Why the design scales
* **Partitioning by VIN** ⇒ per-vehicle state is local to one consumer; adding consumers needs no coordination beyond Kafka's group protocol; ordering per vehicle holds.
* **Batching** (500 messages) amortises Redis/PostgreSQL round trips (pipelines, `executemany`, bulk upserts).
* **Idempotent effects** ⇒ any component can be restarted, rescaled or replayed without a global barrier.
* **Read path decoupled from write path (CQRS)**: dashboards read Redis state/aggregates, materialised views and read models; they never scan raw telemetry.
* **Caching with stampede protection** (2–30 s) for expensive analytics, **keyset pagination** and **partial indexes** for hot queries.
* **Cold analytics off the OLTP database**: Parquet + DuckDB.

## Hot spots and how they are avoided
* Hot Kafka partitions: key = VIN (uniform), not tenant/time.
* Hot Redis keys: per-vehicle keys; fleet aggregates are per tenant-hour hashes (one tenant ⇒ one key, written by many processors with `HINCRBYFLOAT`; acceptable up to the single-key write rate; shard by `vin % 16` suffix if a single tenant dominates).
* Hot PostgreSQL rows: snapshot upserts are per-vehicle and throttled; alerts are insert-only; daily stats use absolute upserts.
* Alert storms: one alert per vehicle/type/severity/5-min bucket (`dedup_key`).

## Capacity (see benchmarks for the measured per-replica values)
The model in `benchmarks/make_report.py` computes replicas, backlog and drain time from the measured events/s/replica. Headline: at ~4.4K events/s per
stream-processor replica (this laptop, PostgreSQL sink) 100K events/s needs ≈ 23 replicas just to keep up, so the cloud profile uses 48 partitions/replicas to
leave burst-recovery headroom. A faster hot loop (Cython/Rust/Go), larger batches, or fewer per-event Redis commands would reduce that number; the
alternative is more replicas.

## Storage growth (estimates from schema sizes — not measurements)
100K vehicles at 1 Hz ≈ 8.6 B events/day; ~400 B JSON each ≈ 3.5 TB/day raw; Parquet+zstd typically 8–12× smaller ⇒ ≈ 300–450 GB/day cold-eligible.
HOT (Mongo, 7 days) at that rate is ~24 TB — in practice vehicles report far below 1 Hz when parked; capacity planning should use the real duty cycle, and the
7-day TTL is a configuration (`RAW_EVENTS_TTL_DAYS`). Relational growth is dominated by `alerts` (bounded by dedup) and `daily_vehicle_stats` (100K rows/day).

## Graceful degradation summary
Redis down → API serves from PostgreSQL, processors fall back to Bloom-filter dedupe; Mongo/Postgres down → back-pressure (Kafka retains up to 24 h);
object store down → buffering; Kafka down → edge 503 so devices retry; consumer lag drives autoscaling instead of data loss.
