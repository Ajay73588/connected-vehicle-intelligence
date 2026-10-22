# Demo script (≈ 8 minutes) — also a checklist for recording the demo video

Prerequisites: stack running (`docker compose up --build`, or the embedded stack), browser at <http://localhost:8080> (or Vite dev server :5173).
Accounts (password `Demo@12345`): `fleet_manager@acme.demo`, `admin@acme.demo`, `analyst@acme.demo`, `fleet_manager@borealis.demo`, `fleet_manager@metro.demo`.
Automated rehearsal of the same flow: `python scripts/e2e.py` (prints the measured alert latency per scenario).

| # | Do | Show / say | Backed by |
|---|---|---|---|
| 1 | Sign in as `fleet_manager@acme.demo` | Tenant badge, role, permissions; 45,000 vehicles belong to this tenant | RBAC, tenant claim |
| 2 | **Dashboard** | KPI tiles refresh every 3 s; events/hour moves while the simulator runs; ML risk table says "synthetic-trained" | Redis aggregates, cached summary |
| 3 | **Live map** | vehicles by severity colour; dashed geofences; click a vehicle → health/risk/prediction | Redis geohash cells, no SQL on the hot path |
| 4 | **Operations → run `engine_failure`** | "40 events published for 1 vehicle" | events go to `telemetry.raw` like any device |
| 5 | **Alerts** (auto-refresh) | within ~1–2 s: critical *Engine overheating*, *Repeated DTC*, *Low oil pressure*; open **evidence**; confidence, cause, recommended action | detectors, `alert_latency_seconds` |
| 6 | Open the vehicle (link) | health score < 40 with the penalty breakdown; signals chart (coolant climbing, oil pressure falling); DTC list P0301/P0217; ML failure probability and its drivers | PostgreSQL read models + Mongo telemetry |
| 7 | **AI Copilot**: "Explain why `<VIN>` is unhealthy" | answer cites tools `get_vehicle_health`, `search_telemetry`, `search_knowledge_base`; recommended actions; click **Audit trail** → every step with hash | agent, audit chain |
| 8 | Copilot: the six canonical questions | maintenance risk ranking, utilisation analysis, battery, repeated engine faults, recommended actions, idle-cost saving (numbers equal the database) | tools |
| 9 | Run `dangerous_driver` (Acme) | risk score 100 → *Dangerous driving*; ask "Which drivers are dangerous?" | driver risk |
| 10 | Sign out, sign in as `fleet_manager@metro.demo`, run `fleet_idling`; ask "How much money could we save by reducing idling?" | 6 buses idle → cost in USD, annualised saving, assumptions listed | fleet-level recommendation |
| 11 | Sign in as `fleet_manager@borealis.demo`, run `ev_battery` | SoH below 70 % → critical battery alert; maintenance recommendation | EV battery |
| 12 | **Tenant isolation:** as Borealis open a VIN from Acme in the copilot | "No vehicle … in your fleet." (404 at the API) | RLS + app filters |
| 13 | Copilot: "Create an alert for `<VIN>`: brake noise" | confirmation card → **Confirm** → alert appears; audit shows `confirmation` steps | destructive-action guard |
| 14 | Sign in as `analyst@acme.demo` | locations are coarse (5 km cell); write buttons hidden; copilot refuses write intents | privacy + RBAC |
| 15 | **Grafana** (compose) | ingestion rate, lag, latency, DLQ reasons, alert rate, API p95/p99 | Prometheus |
| 16 | **Resilience:** `docker compose kill kafka` → edge returns 503 (device back-off); `docker compose start kafka` → traffic resumes; `docker compose kill postgres` → `/health/ready` 503, processors back off; restart → alert delivered once | circuit breakers, back-pressure, idempotency | `make chaos` automates it |
| 17 | **Erasure:** Operations → erase a vehicle (admin) | report: Redis keys, Mongo docs, Parquet rows removed; audit entry; vehicle 404 afterwards | GDPR/DPDP workflow |
| 18 | Show `docs/performance/sql-before-after.md` | 277 s → 92 ms, 243.8 → 0.4 ms real plans | SQL optimisation |

Reset between takes: scenarios can be re-run after ~5 minutes (alert cool-down buckets) or re-seed (`make seed`).
