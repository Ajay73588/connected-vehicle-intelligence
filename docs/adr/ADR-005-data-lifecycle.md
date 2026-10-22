# ADR-005: HOT / WARM / COLD data lifecycle with Parquet partitioning and erasure by design

- Status: accepted
- Date: 2026-10-01

## Context
At 100,000 vehicles × 1 Hz ≈ 8.6 billion events/day (~2.6 TB/day as JSON). Keeping everything in a database is unaffordable; deleting
it loses analytics value; privacy law requires deletion on request. Access patterns decay quickly: seconds–hours for operations,
days for investigations, months for trend analysis and model training.

## Decision
| Tier | Where | Retention | Format | Typical access |
|---|---|---|---|---|
| **HOT** | Redis (latest state, hourly aggregates, dedupe sets: 3 h–3 d TTL) + MongoDB raw events (TTL index **7 days**) | 7 d | JSON documents | live map, vehicle page, agent `search_telemetry` |
| **WARM** | Object storage `telemetry/tier=warm/tenant=T/date=D/hour=H/part-<sha256>.parquet` (zstd, ≈5 s buffers) | ≈ 45 d (bucket rule 45 d; app rule 30 d) | Parquet, sorted by `(vin, ts)` | vehicle history charts, investigations (DuckDB) |
| **COLD** | `telemetry/tier=cold/tenant=T/date=D/day.parquet` — one compacted, de-duplicated daily file per tenant | 730 d; S3 Standard-IA after 30 d, Glacier IR after 180 d | Parquet (zstd, 100k-row groups) | model training, audits, long-range trends |

* **Partitioning:** tenant (isolation, per-tenant erasure/retention), date and hour (time pruning, retention by prefix). *Vehicle is
  deliberately not a path component* (100K vehicles would create millions of tiny files); it is the sort key inside files so Parquet
  row-group statistics prune vehicle lookups.
* **Compaction** (`lifecycle/jobs.py`): the lifecycle service merges a closed day's warm files into one cold file, removes duplicate
  event ids and deletes the warm files only after the cold object exists — idempotent and safe to repeat.
* **Idempotent writes:** file names are content hashes of the event ids they contain, so replaying a batch overwrites rather than duplicates.
* **Erasure (GDPR/DPDP):** `POST /api/v1/admin/erasure` removes a vehicle from Redis, MongoDB, relational tables (cascade) and
  **rewrites every Parquet file** containing it; the vehicle row is kept as a tombstone (`erased_at`) so ingestion rejects its VIN
  afterwards. Driver erasure anonymises the person (name, licence hash) while keeping aggregate safety statistics. Requests, reports
  and the action are written to `erasure_requests` and the hash-chained `audit_log`. Pseudonymised driver data and masked location
  (geohash-5 for roles without `location:precise`) are the default.
* **Cost reasoning:** JSON in Mongo for 7 days is the expensive tier (~18 TB at full scale, hence the short TTL); Parquet+zstd is
  typically 8–12× smaller than JSON, the COLD tier on infrequent-access storage costs a fraction of standard storage and is only
  read by batch jobs. The numbers are estimates from the schema, not measurements (see docs/scaling.md).

## Alternatives considered
* Keep everything in MongoDB/Cassandra with TTL: simple but 10× the storage bill and no open analytic format.
* Delta/Iceberg tables: better table semantics (ACID compaction, schema evolution); chosen not to add a catalog dependency here — the
  Hive-style layout is Iceberg-migratable.
* Per-vehicle files: unbounded file counts.

## Consequences
* Cloud lifecycle rules (Terraform) enforce retention even if the lifecycle service is down.
* Erasure requires rewriting Parquet files (cost proportional to the tenant's retained partitions); acceptable for rare, legally
  mandated operations; an erasure index (vehicle → files) is the optimisation if volume grows.
