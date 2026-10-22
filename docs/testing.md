# Testing

## Pyramid and results (development machine; reproduce with `make test coverage`)
| Layer | Location | What it proves | Count |
|---|---|---|---|
| Unit | `tests/unit` | algorithms (Bloom FP rate, CMS bound, geohash vectors, VIN/OBD, A*=Dijkstra, knapsack = brute force, windows), detectors per scenario, processor idempotency/ordering/DLQ/degradation, resilience (backoff, breaker states), memory broker semantics, schema validation and simulator determinism/chaos coverage, ML (beats baseline, deterministic, explanations), Parquet idempotency/compaction/retention/erasure/DuckDB history, agent planner/guardrails/grounding, JWT/RBAC/masking, edge gateway, stores, infrastructure files (compose/K8s/Terraform/CI/Grafana static checks) | 106 |
| Integration | `tests/integration` | **real PostgreSQL** migrations, constraints, **RLS**, hash-chained audit, idempotent sinks, keyset index use; full HTTP API: auth, lockout, RBAC, tenant isolation (IDOR, filters, agent), pagination, rate limiting, headers/CORS, privacy masking, pipeline-through-API, agent with tools/guardrails/confirmation/audit, erasure across tiers; batch scoring; Lua scripts against a **real Redis**; **chaos/recovery** (DB outage, broker outage, consumer crash, API instance death, Redis outage); Kafka/Mongo tests run when `TEST_KAFKA_BOOTSTRAP`/`TEST_MONGO_URL` are set (CI) | 58 (2 service-gated) |
| Contract | `tests/contract` | published JSON Schemas for alerts, DLQ, agent and REST items; pinned OpenAPI surface; every protected route requires a token | 5 |
| Acceptance (BDD) | `tests/acceptance` (pytest-bdd, Gherkin) | engine failure, dangerous driver, EV battery, fleet idling, duplicates/out-of-order, tenant isolation, confirmation flow | 7 |
| Frontend | `frontend/src/lib.test.tsx` (Vitest) | markdown renderer safety, API client auth/refresh | 5 |
| Load | `tests/load`, `benchmarks/` | k6 ingest (steady + 3× burst), k6 API, Locust; reproducible local benchmarks | — |

Total Python tests in the final local run: **174 passed, 2 skipped** (the two skipped need real Kafka/MongoDB and run in CI); **coverage 86.5%** of `src/cvi`
(branch-insensitive line coverage; excludes `messaging/kafka.py`, the simulator CLI and `__main__` entry points, which need live Kafka / processes).
CI enforces `--cov-fail-under=80`. Coverage is spent on behaviour (see the assertions) rather than on getters.

## How the tests avoid faking
* PostgreSQL tests use a real server: the `pixeltable-pgserver` wheel locally (PostgreSQL 18) or a service container in CI; `TEST_ADMIN_DATABASE_URL` selects it.
  If neither exists the tests are skipped, not mocked.
* Redis Lua scripts are executed by a real Redis server when reachable (`tests/integration/test_scoring_and_redis.py`) and by `fakeredis`'s Lua engine elsewhere.
* The in-memory broker, `fakeredis` and `MemoryRawStore` stand in for Kafka/Redis/MongoDB only in tests and the embedded dev mode; the Kafka and MongoDB adapters have
  dedicated integration tests that need the real services (CI).

## Running
```bash
make setup                       # venv, dependencies, model
make test                        # everything (embedded PostgreSQL if no TEST_ADMIN_DATABASE_URL)
make coverage                    # + 80% gate
pytest tests/acceptance          # BDD only
cd frontend && npm test
```
CI-like integration against real services: `TEST_ADMIN_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres TEST_KAFKA_BOOTSTRAP=localhost:9092 TEST_MONGO_URL=mongodb://localhost:27017 pytest tests`.

## Load tests
* **Ingest (100K events/s target):** `k6 run -e BASE=http://localhost:8001 -e RATE=100 -e BATCH=1000 tests/load/k6_ingest.js` — `RATE` iterations/s × `BATCH` events;
  after `STEADY` (default 5 m) a second scenario runs at 3× for `BURST` (5 m). VINs are valid and distinct (same algorithm as the simulator) so load spreads over partitions.
  Thresholds: p95 < 200 ms, p99 < 500 ms, < 1% failures. One k6 process cannot generate 100K events/s with JSON bodies — run several load generators (k6 operator / cloud).
  Alternative generator: `python services/simulator/simulator.py --mode 100k --sink kafka --duration 300` (multi-process, writes straight to Kafka).
* **API:** `k6 run tests/load/k6_api.js` (p95 < 200 ms, p99 < 500 ms thresholds), `locust -f tests/load/locustfile.py`.
* **Measure** throughput, p50/p95/p99 (k6 output), consumer lag (`consumer_lag` metric / Grafana), errors (`events_failed_total`, DLQ), CPU/memory (`docker stats`, `kubectl top`), DB latency (`pg_stat_statements`).
* **Status:** the 100K events/s run was **not executed** (needs a Kafka cluster and several generators); local single-process measurements and the capacity model are in
  `docs/performance/benchmarks.md`. **Soak test:** `SIM_MODE=small` for hours with Grafana open; watch RSS of `stream-processor`, lag, and Redis memory (3 h seen-set TTL bounds growth) — not executed locally.

## Chaos / recovery
In-process chaos tests run everywhere (`tests/integration/test_chaos.py`). Against a real stack: `make chaos` (`scripts/chaos_compose.sh`) kills Kafka, the stream processor,
PostgreSQL, Redis and an API replica in turn, asserting health reporting and that `scripts/e2e.py` passes after recovery (CI job `e2e`; not run locally — no Docker).

## Security testing
SAST (Bandit; Semgrep in CI), dependency scans (pip-audit, npm audit), container/IaC scans (Trivy in CI), DAST (OWASP ZAP API scan in CI). Local results: `docs/security/scan-results.md`.
