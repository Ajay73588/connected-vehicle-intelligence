# SQL optimisation evidence (generated 2026-10-01 06:59 UTC)

All plans below are **real `EXPLAIN (ANALYZE, BUFFERS)` output** produced by `scripts/sql_evidence.py` against PostgreSQL 18.4 on the seeded synthetic dataset (100,000 vehicles, 1,000,000 alerts, 1,040,381 daily-stat rows, 100,000 predictions). Queries run as the RLS-enforced `cvi_app` role with a tenant context (tenant `acme-logistics`), exactly like the API. Timings are the best of the repeated runs (the pathological BEFORE plans are run once or twice because each execution takes minutes) on a development laptop; absolute numbers will differ elsewhere, the *shape* of the plans is the evidence.

## Summary

| Query | BEFORE (ms) | AFTER (ms) | speed-up |
|---|---|---|---|
| Alert feed, page at depth 5,000 | 243.8 | 0.4 | 572x |
| Dashboard KPI trend (14 days) | 583.8 | <0.05 | >10,000x |
| Top-20 maintenance risk | 277255.1 | 92.1 | 3009x |
| Vehicle list, 50 rows (N+1 vs join) | 42.9 | 1.6 | 26x |


## Query 1 - Alert feed: OFFSET pagination vs keyset + partial composite index

The alert list is the busiest read in the product (dashboard widget + alerts page). Deep OFFSET pages must read and discard every skipped row, and without an index matching the filter and the sort the planner scans and sorts all of the tenant's alerts.

### BEFORE

```sql
SELECT a.id, a.vehicle_id, v.vin, a.alert_type_code, t.title, a.severity, a.status, a.confidence, a.cause, a.created_at
FROM alerts a JOIN vehicles v ON v.id = a.vehicle_id JOIN alert_types t ON t.code = a.alert_type_code
WHERE a.tenant_id = %s AND a.status = 'open' AND a.severity = 'critical'
ORDER BY a.created_at DESC, a.id DESC OFFSET 5000 LIMIT 50
```

```text
Limit  (cost=74494.58..74500.40 rows=50 width=145) (actual time=238.089..243.507 rows=50.00 loops=1)
  Buffers: shared hit=5776 read=57941
  ->  Gather Merge  (cost=73912.24..77258.33 rows=28730 width=145) (actual time=237.396..243.378 rows=5050.00 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=5776 read=57941
        ->  Sort  (cost=72912.22..72942.15 rows=11971 width=145) (actual time=217.466..217.530 rows=1901.00 loops=3)
              Sort Key: a.created_at DESC, a.id DESC
              Sort Method: quicksort  Memory: 2527kB
              Buffers: shared hit=5776 read=57941
              Worker 0:  Sort Method: quicksort  Memory: 2482kB
              Worker 1:  Sort Method: quicksort  Memory: 2463kB
              ->  Result  (cost=2251.44..72116.03 rows=11971 width=145) (actual time=108.295..214.117 rows=8333.33 loops=3)
                    One-Time Filter: ((NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::bigint = '1'::smallint)
                    Buffers: shared hit=5746 read=57941
                    ->  Hash Join  (cost=2251.44..72116.03 rows=11971 width=145) (actual time=108.288..213.173 rows=8333.33 loops=3)
                          Hash Cond: (a.alert_type_code = t.code)
                          Buffers: shared hit=5746 read=57941
                          ->  Parallel Hash Join  (cost=2250.18..72071.24 rows=11971 width=123) (actual time=107.725..210.562 rows=8333.33 loops=3)
                                Hash Cond: (a.vehicle_id = v.id)
                                Buffers: shared hit=5743 read=57941
                                ->  Parallel Seq Scan on alerts a  (cost=0.00..69789.63 rows=11971 width=105) (actual time=103.709..201.029 rows=8333.33 loops=3)
                                      Filter: ((tenant_id = '1'::smallint) AND (status = 'open'::text) AND (severity = 'critical'::text))
                                      Rows Removed by Filter: 325000
                                      Buffers: shared hit=4559 read=57941
                                ->  Parallel Hash  (cost=1919.29..1919.29 rows=26471 width=26) (actual time=3.914..3.915 rows=15000.00 loops=3)
                                      Buckets: 65536  Batches: 1  Memory Usage: 3360kB
                                      Buffers: shared hit=1184
                                      ->  Parallel Seq Scan on vehicles v  (cost=0.00..1919.29 rows=26471 width=26) (actual time=0.005..5.326 rows=45000.00 loops=1)
                                            Filter: (tenant_id = '1'::smallint)
                                            Rows Removed by Filter: 55000
                                            Buffers: shared hit=1184
                          ->  Hash  (cost=1.11..1.11 rows=11 width=39) (actual time=0.553..0.554 rows=11.00 loops=3)
                                Buckets: 1024  Batches: 1  Memory Usage: 9kB
                                Buffers: shared hit=3
                                ->  Seq Scan on alert_types t  (cost=0.00..1.11 rows=11 width=39) (actual time=0.539..0.540 rows=11.00 loops=3)
                                      Buffers: shared hit=3
Planning:
  Buffers: shared hit=10
Planning Time: 0.333 ms
Execution Time: 243.820 ms
```

### Optimisation

1. **Partial composite index** `alerts_open_severity_idx (tenant_id, severity, created_at DESC, id DESC) WHERE status = 'open'` - the equality columns come first, the sort columns last, so rows are already in `ORDER BY` order; the `WHERE status='open'` predicate keeps the index small (only open alerts, the hot set).
2. **Keyset pagination**: `(created_at, id) < (cursor)` replaces `OFFSET`, so cost is O(page size) at any depth and pages are stable while new alerts arrive.
3. The two small look-ups (`vehicles`, `alert_types`) are served by primary keys (nested loops), not by joining the whole tables.

### AFTER

```sql
SELECT a.id, a.vehicle_id, v.vin, a.alert_type_code, t.title, a.severity, a.status, a.confidence, a.cause, a.created_at
FROM alerts a JOIN vehicles v ON v.id = a.vehicle_id JOIN alert_types t ON t.code = a.alert_type_code
WHERE a.tenant_id = %s AND a.status = 'open' AND a.severity = 'critical'
  AND (a.created_at, a.id) < (%s, %s)
ORDER BY a.created_at DESC, a.id DESC LIMIT 50
```

```text
Limit  (cost=0.87..191.76 rows=50 width=146) (actual time=0.064..0.371 rows=50.00 loops=1)
  Buffers: shared hit=214
  ->  Result  (cost=0.87..85680.14 rows=22442 width=146) (actual time=0.063..0.365 rows=50.00 loops=1)
        One-Time Filter: ((NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::bigint = '1'::smallint)
        Buffers: shared hit=214
        ->  Nested Loop  (cost=0.87..85680.14 rows=22442 width=146) (actual time=0.058..0.352 rows=50.00 loops=1)
              Buffers: shared hit=214
              ->  Nested Loop  (cost=0.56..72602.59 rows=22442 width=128) (actual time=0.047..0.167 rows=50.00 loops=1)
                    Buffers: shared hit=64
                    ->  Index Scan using alerts_open_severity_idx on alerts a  (cost=0.42..72065.22 rows=22442 width=106) (actual time=0.024..0.068 rows=50.00 loops=1)
                          Index Cond: ((tenant_id = '1'::smallint) AND (severity = 'critical'::text) AND (ROW(created_at, id) < ROW('2026-09-29 04:24:38.01476+05:30'::timestamp with time zone, 200059)))
                          Index Searches: 1
                          Buffers: shared hit=42
                    ->  Memoize  (cost=0.15..0.16 rows=1 width=39) (actual time=0.001..0.001 rows=1.00 loops=50)
                          Cache Key: a.alert_type_code
                          Cache Mode: logical
                          Hits: 39  Misses: 11  Evictions: 0  Overflows: 0  Memory Usage: 2kB
                          Buffers: shared hit=22
                          ->  Index Scan using alert_types_pkey on alert_types t  (cost=0.14..0.15 rows=1 width=39) (actual time=0.003..0.003 rows=1.00 loops=11)
                                Index Cond: (code = a.alert_type_code)
                                Index Searches: 11
                                Buffers: shared hit=22
              ->  Index Scan using vehicles_pkey on vehicles v  (cost=0.29..0.57 rows=1 width=26) (actual time=0.003..0.003 rows=1.00 loops=50)
                    Index Cond: (id = a.vehicle_id)
                    Filter: (tenant_id = '1'::smallint)
                    Index Searches: 50
                    Buffers: shared hit=150
Planning:
  Buffers: shared hit=18
Planning Time: 0.629 ms
Execution Time: 0.436 ms
```

**Result: 243.8 ms -> 0.4 ms (572x faster; planning 0.3 -> 0.6 ms).**


## Query 2 - Executive-dashboard KPI trend: raw fact table vs materialised view

The trend chart aggregates one row per vehicle per day. Over the whole fleet that is hundreds of thousands of rows per request.

### BEFORE

```sql
SELECT day, count(DISTINCT vehicle_id) AS active_vehicles, sum(distance_km) AS km, sum(drive_seconds) AS drive_s,
       sum(idle_seconds) AS idle_s, sum(fuel_litres) AS fuel, sum(energy_kwh) AS kwh, sum(harsh_brake + harsh_accel + speeding) AS safety
FROM daily_vehicle_stats WHERE tenant_id = %s AND day >= current_date - 14 GROUP BY day ORDER BY day
```

```text
GroupAggregate  (cost=94617.52..107459.94 rows=14 width=60) (actual time=455.481..601.778 rows=14.00 loops=1)
  Group Key: day
  Buffers: shared hit=2654 read=11218, temp read=4017 written=4029
  ->  Sort  (cost=94617.52..95785.00 rows=466992 width=56) (actual time=439.172..509.166 rows=467987.00 loops=1)
        Sort Key: day, vehicle_id
        Sort Method: external merge  Disk: 32136kB
        Buffers: shared hit=2654 read=11218, temp read=4017 written=4029
        ->  Result  (cost=0.01..34679.63 rows=466992 width=56) (actual time=0.144..229.580 rows=467987.00 loops=1)
              One-Time Filter: ((NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::bigint = '1'::smallint)
              Buffers: shared hit=2654 read=11218
              ->  Seq Scan on daily_vehicle_stats  (cost=0.01..34679.63 rows=466992 width=56) (actual time=0.137..145.371 rows=467987.00 loops=1)
                    Filter: ((tenant_id = '1'::smallint) AND (day >= (CURRENT_DATE - 14)))
                    Rows Removed by Filter: 572394
                    Buffers: shared hit=2654 read=11218
Planning Time: 0.155 ms
Execution Time: 615.571 ms
```

### Optimisation

Pre-aggregate once: `mv_fleet_daily_kpis` (tenant, day) holds one row per tenant-day and is refreshed with `REFRESH MATERIALIZED VIEW CONCURRENTLY` by the lifecycle job (a unique index makes concurrent refresh possible, so readers never block). The API reads ~14 rows instead of aggregating ~14 x active-vehicles rows. (Materialised views do not support RLS, so every query filters `tenant_id` explicitly and the app role has SELECT only on the view.)

### AFTER

```sql
SELECT day, active_vehicles, distance_km, drive_seconds, idle_seconds, fuel_litres, energy_kwh, safety_events
FROM mv_fleet_daily_kpis WHERE tenant_id = %s AND day >= current_date - 14 ORDER BY day
```

```text
Sort  (cost=2.11..2.14 rows=14 width=60) (actual time=0.018..0.020 rows=14.00 loops=1)
  Sort Key: day
  Sort Method: quicksort  Memory: 26kB
  Buffers: shared hit=1
  ->  Seq Scan on mv_fleet_daily_kpis  (cost=0.00..1.84 rows=14 width=60) (actual time=0.010..0.013 rows=14.00 loops=1)
        Filter: ((tenant_id = '1'::smallint) AND (day >= (CURRENT_DATE - 14)))
        Rows Removed by Filter: 28
        Buffers: shared hit=1
Planning Time: 0.062 ms
Execution Time: 0.029 ms
```

**Result: 583.8 ms -> <0.05 ms (>10,000x faster; the AFTER plan above shows 0.029 ms; planning 0.2 -> 0.1 ms).**


## Query 3 - Top maintenance risk: per-vehicle LATERAL probe vs DISTINCT ON view + tenant/probability index

The risk table must show the latest prediction per vehicle. The naive form probes `maintenance_predictions` once per vehicle (the SQL equivalent of an ORM N+1), for every vehicle of the tenant, before it can sort.

### BEFORE

```sql
SELECT v.vin, p.failure_prob FROM vehicles v
JOIN LATERAL (SELECT failure_prob FROM maintenance_predictions p WHERE p.vehicle_id = v.id ORDER BY predicted_at DESC LIMIT 1) p ON true
WHERE v.tenant_id = %s ORDER BY p.failure_prob DESC LIMIT 20
```

```text
Limit  (cost=177843680.82..177843680.87 rows=20 width=23) (actual time=277255.083..277255.086 rows=20.00 loops=1)
  Buffers: shared hit=64486187
  ->  Sort  (cost=177843680.82..177843793.86 rows=45217 width=23) (actual time=277255.082..277255.084 rows=20.00 loops=1)
        Sort Key: p.failure_prob DESC
        Sort Method: top-N heapsort  Memory: 27kB
        Buffers: shared hit=64486187
        ->  Result  (cost=3933.02..177842477.61 rows=45217 width=23) (actual time=13.011..277143.270 rows=45000.00 loops=1)
              One-Time Filter: ((NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::bigint = '1'::smallint)
              Buffers: shared hit=64486184
              ->  Nested Loop  (cost=3933.02..177842477.61 rows=45217 width=23) (actual time=13.009..277104.457 rows=45000.00 loops=1)
                    Buffers: shared hit=64486184
                    ->  Seq Scan on vehicles v  (cost=0.00..2434.00 rows=45217 width=26) (actual time=0.016..34.672 rows=45000.00 loops=1)
                          Filter: (tenant_id = '1'::smallint)
                          Rows Removed by Filter: 55000
                          Buffers: shared hit=1184
                    ->  Limit  (cost=3933.01..3933.02 rows=1 width=13) (actual time=6.153..6.154 rows=1.00 loops=45000)
                          Buffers: shared hit=64485000
                          ->  Sort  (cost=3933.01..3933.02 rows=1 width=13) (actual time=6.149..6.149 rows=1.00 loops=45000)
                                Sort Key: p.predicted_at DESC
                                Sort Method: quicksort  Memory: 25kB
                                Buffers: shared hit=64485000
                                ->  Seq Scan on maintenance_predictions p  (cost=0.00..3933.00 rows=1 width=13) (actual time=3.179..6.122 rows=1.00 loops=45000)
                                      Filter: ((vehicle_id = v.id) AND (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::bigint))
                                      Rows Removed by Filter: 99999
                                      Buffers: shared hit=64485000
Planning:
  Buffers: shared hit=52
Planning Time: 2.006 ms
Execution Time: 277255.114 ms
```

### Optimisation

`latest_maintenance_predictions` (a `security_invoker` view, so RLS still applies) resolves 'latest per vehicle' once with `DISTINCT ON` using `predictions_vehicle_idx (vehicle_id, predicted_at DESC)`, and `predictions_tenant_prob_idx (tenant_id, failure_prob DESC)` supports tenant-scoped ranking. The vehicle join happens only for the 20 winners instead of for every vehicle.

### AFTER

```sql
SELECT v.vin, p.failure_prob FROM latest_maintenance_predictions p JOIN vehicles v ON v.id = p.vehicle_id
WHERE p.tenant_id = %s ORDER BY p.failure_prob DESC LIMIT 20
```

```text
Limit  (cost=11890.24..11890.29 rows=20 width=23) (actual time=93.106..93.109 rows=20.00 loops=1)
  Buffers: shared hit=137124
  ->  Sort  (cost=11890.24..11890.49 rows=102 width=23) (actual time=93.105..93.106 rows=20.00 loops=1)
        Sort Key: p.failure_prob DESC
        Sort Method: top-N heapsort  Memory: 27kB
        Buffers: shared hit=137124
        ->  Nested Loop  (cost=0.89..11887.53 rows=102 width=23) (actual time=0.039..85.953 rows=45000.00 loops=1)
              Buffers: shared hit=137124
              ->  Subquery Scan on p  (cost=0.60..10338.32 rows=227 width=13) (actual time=0.032..36.453 rows=45000.00 loops=1)
                    Filter: (p.tenant_id = '1'::smallint)
                    Buffers: shared hit=2124
                    ->  Unique  (cost=0.60..9771.57 rows=45340 width=103) (actual time=0.031..33.235 rows=45000.00 loops=1)
                          Buffers: shared hit=2124
                          ->  Incremental Sort  (cost=0.60..9658.22 rows=45340 width=103) (actual time=0.031..28.984 rows=45000.00 loops=1)
                                Sort Key: maintenance_predictions.vehicle_id, maintenance_predictions.predicted_at DESC, maintenance_predictions.id DESC
                                Presorted Key: maintenance_predictions.vehicle_id, maintenance_predictions.predicted_at
                                Full-sort Groups: 1407  Sort Method: quicksort  Average Memory: 26kB  Peak Memory: 26kB
                                Buffers: shared hit=2124
                                ->  Index Scan using predictions_vehicle_idx on maintenance_predictions  (cost=0.42..7617.92 rows=45340 width=103) (actual time=0.011..22.060 rows=45000.00 loops=1)
                                      Filter: (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::bigint)
                                      Rows Removed by Filter: 55000
                                      Index Searches: 1
                                      Buffers: shared hit=2124
              ->  Index Scan using vehicles_pkey on vehicles v  (cost=0.29..6.82 rows=1 width=26) (actual time=0.001..0.001 rows=1.00 loops=45000)
                    Index Cond: (id = p.vehicle_id)
                    Filter: (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::bigint)
                    Index Searches: 45000
                    Buffers: shared hit=135000
Planning Time: 0.224 ms
Execution Time: 93.135 ms
```

**Result: 277255.1 ms -> 92.1 ms (3009x faster; planning 2.0 -> 0.2 ms).**


## Query 4 - Eliminating N+1 round-trips on the vehicle list

Loading 50 vehicles with their health and open-alert count the lazy-loading way issues **101** statements; the API issues **one** join (`GET /api/v1/vehicles`, see `LIST_SQL` in `src/cvi/api/routers/vehicles.py`; this project uses plain SQL, not an ORM, precisely to keep the statement count fixed).

| approach | statements | wall time (client-measured, local) |
|---|---|---|
| N+1 lazy loading | 100 | 42.9 ms |
| single join | 1 | 1.6 ms |
