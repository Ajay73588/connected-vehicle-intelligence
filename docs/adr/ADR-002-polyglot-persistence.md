# ADR-002: Polyglot persistence — PostgreSQL + MongoDB + Redis + object storage (Parquet) + pgvector

- Status: accepted
- Date: 2026-10-01

## Context
Different data in this system has opposite requirements: money/ownership/access control need ACID and constraints; raw telemetry is
a firehose of schema-variable, append-only documents; dashboards need millisecond reads of "current" state; history is scanned in
bulk; knowledge retrieval needs similarity search. One engine cannot be optimal for all five.

## Decision
| Data | Store | Why this store | Consistency class |
|---|---|---|---|
| tenants, users, roles, fleets, vehicles, drivers, subscriptions, alerts, maintenance, predictions, audit | **PostgreSQL 16** (3NF, FK/CHECK, RLS) | ACID, constraints, joins, row-level security, mature tooling | strong (CP) |
| embeddings of knowledge docs and incidents | **pgvector** in the same PostgreSQL | semantic search next to the relational data it joins to; tenant RLS applies; no second operational system | strong |
| raw telemetry events (7 days, HOT) | **MongoDB** (`_id = event_id`, TTL index, shard key `{tenant, hashed vin}`) | schema differs per powertrain/firmware (EV vs ICE vs hybrid), high write rate, append-only, TTL expiry, read by (vin, time) | eventual (AP) |
| latest vehicle state, dedupe sets, hourly aggregates, map cells, rate-limit buckets, caches | **Redis** | O(1) reads/writes, atomic Lua, TTLs, geohash cell sets for the live map | eventual |
| WARM/COLD history | **S3-compatible object storage, Parquet (zstd), Hive partitions** `tenant/date/hour`, queried with **DuckDB** | cheapest per GB, columnar scans, immutable files, retention by prefix | eventual |

Why MongoDB at all, given PostgreSQL could hold JSONB? At 100K events/s a relational table would need partitioning, vacuuming
and WAL capacity for data that is never updated and expires after a week; a document store with a TTL index and hashed sharding is
the simpler operational fit. It also keeps the OLTP database free of bursty telemetry I/O (noisy-neighbour isolation).
Why vectors in PostgreSQL rather than a dedicated vector DB? Corpus size is small (knowledge docs + one embedding per alert),
tenant isolation and joins come for free; a dedicated engine would add an operational dependency without a measurable benefit.

## Alternatives considered
* Everything in PostgreSQL (+TimescaleDB): simplest operationally, but couples OLTP latency to telemetry volume.
* Cassandra for telemetry: excellent write scaling, but heavier to run on a laptop and no TTL/aggregation convenience we need.
* ClickHouse for analytics: great, but DuckDB-over-Parquet already covers the "historical/batch analytics" need at zero ops cost
  and keeps the cold tier in an open format.
* Qdrant/Pinecone: see above.

## Trade-offs / consequences
* More moving parts; mitigated by one `docker compose up`, health checks and a single configuration surface.
* Cross-store consistency is by design **eventual** for derived data. Rules: PostgreSQL is the system of record for
  business entities; every telemetry sink is idempotent (Mongo `_id`, content-addressed Parquet names, `UNIQUE dedup_key` alerts,
  absolute-value upserts for daily stats, Redis Lua gate for aggregates); GDPR erasure fans out to all stores
  (`src/cvi/lifecycle/erasure.py`).
* Embedded dev/test mode substitutes in-process emulators for Redis/MongoDB (`fakeredis`, `MemoryRawStore`); they are never used by
  deployed services.
* Local note: the Windows pgvector build bundled with the embedded dev PostgreSQL crashes on 256-dimension distance operators
  (illegal CPU instruction), so the dev environment uses `VECTOR_BACKEND=python` (client-side cosine). Docker/CI/production use the
  real pgvector operators and HNSW indexes (`migrations/0006_vector_hnsw.sql`).
