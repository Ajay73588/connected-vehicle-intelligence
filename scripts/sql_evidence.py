#!/usr/bin/env python
"""Generate docs/performance/sql-before-after.md from REAL EXPLAIN (ANALYZE, BUFFERS) runs on the seeded 100K-vehicle database.

Executes as the RLS-enforced application role with a tenant context (what the API really runs), drops/recreates the
optimised indexes to produce the BEFORE/AFTER plans, and records exact timings. Nothing in the report is typed by hand."""

from __future__ import annotations

import datetime as dt
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from cvi.common.config import get_settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OPT_INDEXES = {
    "alerts_tenant_created_idx": "CREATE INDEX alerts_tenant_created_idx ON alerts (tenant_id, created_at DESC, id DESC)",
    "alerts_open_severity_idx": "CREATE INDEX alerts_open_severity_idx ON alerts (tenant_id, severity, created_at DESC, id DESC) WHERE status = 'open'",
    "alerts_vehicle_idx": "CREATE INDEX alerts_vehicle_idx ON alerts (vehicle_id, created_at DESC)",
    "daily_stats_tenant_day_idx": "CREATE INDEX daily_stats_tenant_day_idx ON daily_vehicle_stats (tenant_id, day)",
    "vehicles_tenant_status_idx": "CREATE INDEX vehicles_tenant_status_idx ON vehicles (tenant_id, status, id)",
    "predictions_vehicle_idx": "CREATE INDEX predictions_vehicle_idx ON maintenance_predictions (vehicle_id, predicted_at DESC)",
    "predictions_tenant_prob_idx": "CREATE INDEX predictions_tenant_prob_idx ON maintenance_predictions (tenant_id, failure_prob DESC)",
}


def explain(conn, sql: str, params: list, runs: int = 2) -> tuple[str, float, float]:
    """Return (plan text of the last run, best execution ms, planning ms). One warm-up run first."""
    best, plan_txt, planning = float("inf"), "", 0.0
    for _ in range(runs + 1):
        rows = conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params).fetchall()
        txt = "\n".join(next(iter(r.values())) if isinstance(r, dict) else r[0] for r in rows)
        ex = float(re.search(r"Execution Time: ([\d.]+) ms", txt).group(1))
        planning = float(re.search(r"Planning Time: ([\d.]+) ms", txt).group(1))
        best, plan_txt = min(best, ex), txt
    return plan_txt, best, planning


def main() -> None:
    s = get_settings()
    admin = psycopg.connect(s.admin_database_url, autocommit=True)
    app = psycopg.connect(s.database_url, autocommit=True)  # no lingering transaction: DROP INDEX needs an exclusive lock
    tenant = admin.execute("SELECT id FROM tenants WHERE slug = 'acme-logistics'").fetchone()[0]
    app.execute("SELECT set_config('app.tenant_id', %s, false)", [str(tenant)])
    counts = {t: admin.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("vehicles", "alerts", "daily_vehicle_stats", "maintenance_predictions")}
    print("row counts:", counts)

    # ----- Query 1: alert feed (keyset vs OFFSET) --------------------------------------------------------------------
    q1_before = """SELECT a.id, a.vehicle_id, v.vin, a.alert_type_code, t.title, a.severity, a.status, a.confidence, a.cause, a.created_at
FROM alerts a JOIN vehicles v ON v.id = a.vehicle_id JOIN alert_types t ON t.code = a.alert_type_code
WHERE a.tenant_id = %s AND a.status = 'open' AND a.severity = 'critical'
ORDER BY a.created_at DESC, a.id DESC OFFSET 5000 LIMIT 50"""
    q1_after = """SELECT a.id, a.vehicle_id, v.vin, a.alert_type_code, t.title, a.severity, a.status, a.confidence, a.cause, a.created_at
FROM alerts a JOIN vehicles v ON v.id = a.vehicle_id JOIN alert_types t ON t.code = a.alert_type_code
WHERE a.tenant_id = %s AND a.status = 'open' AND a.severity = 'critical'
  AND (a.created_at, a.id) < (%s, %s)
ORDER BY a.created_at DESC, a.id DESC LIMIT 50"""

    # ----- Query 2: dashboard KPI trend (raw fact table vs materialised view) ----------------------------------------
    q2_before = """SELECT day, count(DISTINCT vehicle_id) AS active_vehicles, sum(distance_km) AS km, sum(drive_seconds) AS drive_s,
       sum(idle_seconds) AS idle_s, sum(fuel_litres) AS fuel, sum(energy_kwh) AS kwh, sum(harsh_brake + harsh_accel + speeding) AS safety
FROM daily_vehicle_stats WHERE tenant_id = %s AND day >= current_date - 14 GROUP BY day ORDER BY day"""
    q2_after = """SELECT day, active_vehicles, distance_km, drive_seconds, idle_seconds, fuel_litres, energy_kwh, safety_events
FROM mv_fleet_daily_kpis WHERE tenant_id = %s AND day >= current_date - 14 ORDER BY day"""

    # ----- Query 3: top maintenance risk (per-vehicle subquery vs DISTINCT ON view with index) -----------------------
    q3_before = """SELECT v.vin, p.failure_prob FROM vehicles v
JOIN LATERAL (SELECT failure_prob FROM maintenance_predictions p WHERE p.vehicle_id = v.id ORDER BY predicted_at DESC LIMIT 1) p ON true
WHERE v.tenant_id = %s ORDER BY p.failure_prob DESC LIMIT 20"""
    q3_after = """SELECT v.vin, p.failure_prob FROM latest_maintenance_predictions p JOIN vehicles v ON v.id = p.vehicle_id
WHERE p.tenant_id = %s ORDER BY p.failure_prob DESC LIMIT 20"""

    out: list[str] = []
    out.append(f"# SQL optimisation evidence (generated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC)\n")
    out.append("All plans below are **real `EXPLAIN (ANALYZE, BUFFERS)` output** produced by `scripts/sql_evidence.py` against PostgreSQL "
               f"{admin.execute('show server_version').fetchone()[0]} on the seeded synthetic dataset "
               f"({counts['vehicles']:,} vehicles, {counts['alerts']:,} alerts, {counts['daily_vehicle_stats']:,} daily-stat rows, "
               f"{counts['maintenance_predictions']:,} predictions). Queries run as the RLS-enforced `cvi_app` role with a tenant context "
               "(tenant `acme-logistics`), exactly like the API. Timings are the best of the repeated runs (the pathological BEFORE plans are run once or twice because each execution takes minutes) on a development laptop; absolute numbers "
               "will differ elsewhere, the *shape* of the plans is the evidence.\n")

    def section(title, why, before_sql, before_params, after_sql, after_params, prep_before, prep_after, rationale, before_runs=1):
        prep_before()
        pb, tb, plb = explain(app, before_sql, before_params, runs=before_runs)  # pathological plans: fewer repetitions
        prep_after()
        pa, ta, pla = explain(app, after_sql, after_params, runs=3)
        out.append(f"\n## {title}\n\n{why}\n\n### BEFORE\n\n```sql\n{before_sql}\n```\n\n```text\n{pb}\n```\n\n"
                   f"### Optimisation\n\n{rationale}\n\n### AFTER\n\n```sql\n{after_sql}\n```\n\n```text\n{pa}\n```\n\n"
                   f"**Result: {tb:.1f} ms -> {ta:.1f} ms ({tb / max(ta, 0.001):.0f}x faster; planning {plb:.1f} -> {pla:.1f} ms).**\n")
        print(f"{title}: {tb:.1f} ms -> {ta:.1f} ms")
        return tb, ta

    def drop(*names):
        for n in names:
            admin.execute(f"DROP INDEX IF EXISTS {n}")

    def create(*names):
        for n in names:
            admin.execute(OPT_INDEXES[n].replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS"))
        admin.execute("ANALYZE")

    # cursor position for the keyset query: the row at depth 5000 of the same ordering
    admin.execute("SET enable_seqscan = on")
    create("alerts_tenant_created_idx", "alerts_open_severity_idx")
    cur = admin.execute("SELECT created_at, id FROM alerts WHERE tenant_id = %s AND status = 'open' AND severity = 'critical' "
                        "ORDER BY created_at DESC, id DESC OFFSET 5000 LIMIT 1", [tenant]).fetchone()
    summary = []
    summary.append(("Alert feed, page at depth 5,000", *section(
        "Query 1 - Alert feed: OFFSET pagination vs keyset + partial composite index",
        "The alert list is the busiest read in the product (dashboard widget + alerts page). Deep OFFSET pages must read and discard every skipped row, "
        "and without an index matching the filter and the sort the planner scans and sorts all of the tenant's alerts.",
        q1_before, [tenant], q1_after, [tenant, cur[0], cur[1]],
        lambda: drop("alerts_tenant_created_idx", "alerts_open_severity_idx", "alerts_vehicle_idx") or admin.execute("ANALYZE alerts"),
        lambda: create("alerts_tenant_created_idx", "alerts_open_severity_idx", "alerts_vehicle_idx"),
        "1. **Partial composite index** `alerts_open_severity_idx (tenant_id, severity, created_at DESC, id DESC) WHERE status = 'open'` - the equality columns come "
        "first, the sort columns last, so rows are already in `ORDER BY` order; the `WHERE status='open'` predicate keeps the index small "
        "(only open alerts, the hot set).\n2. **Keyset pagination**: `(created_at, id) < (cursor)` replaces `OFFSET`, so cost is O(page size) at any depth "
        "and pages are stable while new alerts arrive.\n3. The two small look-ups (`vehicles`, `alert_types`) are served by primary keys (nested loops), "
        "not by joining the whole tables.")))
    summary.append(("Dashboard KPI trend (14 days)", *section(
        "Query 2 - Executive-dashboard KPI trend: raw fact table vs materialised view",
        "The trend chart aggregates one row per vehicle per day. Over the whole fleet that is hundreds of thousands of rows per request.",
        q2_before, [tenant], q2_after, [tenant],
        lambda: drop("daily_stats_tenant_day_idx") or admin.execute("ANALYZE daily_vehicle_stats"),
        lambda: create("daily_stats_tenant_day_idx"),
        "Pre-aggregate once: `mv_fleet_daily_kpis` (tenant, day) holds one row per tenant-day and is refreshed with "
        "`REFRESH MATERIALIZED VIEW CONCURRENTLY` by the lifecycle job (a unique index makes concurrent refresh possible, so readers never block). "
        "The API reads ~14 rows instead of aggregating ~14 x active-vehicles rows. (Materialised views do not support RLS, so every query filters "
        "`tenant_id` explicitly and the app role has SELECT only on the view.)")))
    summary.append(("Top-20 maintenance risk", *section(
        "Query 3 - Top maintenance risk: per-vehicle LATERAL probe vs DISTINCT ON view + tenant/probability index",
        "The risk table must show the latest prediction per vehicle. The naive form probes `maintenance_predictions` once per vehicle (the SQL "
        "equivalent of an ORM N+1), for every vehicle of the tenant, before it can sort.",
        q3_before, [tenant], q3_after, [tenant],
        lambda: drop("predictions_vehicle_idx", "predictions_tenant_prob_idx") or admin.execute("ANALYZE maintenance_predictions"),
        lambda: create("predictions_vehicle_idx", "predictions_tenant_prob_idx"),
        "`latest_maintenance_predictions` (a `security_invoker` view, so RLS still applies) resolves 'latest per vehicle' once with `DISTINCT ON` "
        "using `predictions_vehicle_idx (vehicle_id, predicted_at DESC)`, and `predictions_tenant_prob_idx (tenant_id, failure_prob DESC)` supports "
        "tenant-scoped ranking. The vehicle join happens only for the 20 winners instead of for every vehicle.", before_runs=0)))

    # ----- N+1 demonstration with real round-trips -------------------------------------------------------------------
    ids = [r[0] for r in app.execute("SELECT id FROM vehicles WHERE tenant_id = %s ORDER BY id LIMIT 50", [tenant]).fetchall()]
    t0 = time.perf_counter()
    for vid in ids:  # ORM-style lazy loading: 1 query for vehicles + 1 per vehicle for health + 1 for open alerts
        app.execute("SELECT health_score FROM vehicle_health WHERE vehicle_id = %s", [vid]).fetchone()
        app.execute("SELECT count(*) FROM alerts WHERE vehicle_id = %s AND status = 'open'", [vid]).fetchone()
    n_plus_1 = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    app.execute("""SELECT v.id, h.health_score, (SELECT count(*) FROM alerts a WHERE a.vehicle_id = v.id AND a.status = 'open') AS open_alerts
                   FROM vehicles v LEFT JOIN vehicle_health h ON h.vehicle_id = v.id WHERE v.tenant_id = %s ORDER BY v.id LIMIT 50""", [tenant]).fetchall()
    joined = (time.perf_counter() - t0) * 1000
    out.append(f"\n## Query 4 - Eliminating N+1 round-trips on the vehicle list\n\nLoading 50 vehicles with their health and open-alert count "
               f"the lazy-loading way issues **{1 + 2 * len(ids)}** statements; the API issues **one** join (`GET /api/v1/vehicles`, see `LIST_SQL` in "
               f"`src/cvi/api/routers/vehicles.py`; this project uses plain SQL, not an ORM, precisely to keep the statement count fixed).\n\n"
               f"| approach | statements | wall time (client-measured, local) |\n|---|---|---|\n| N+1 lazy loading | {2 * len(ids)} | {n_plus_1:.1f} ms |\n"
               f"| single join | 1 | {joined:.1f} ms |\n")
    summary.append(("Vehicle list, 50 rows (N+1 vs join)", n_plus_1, joined))

    out.insert(2, "## Summary\n\n| Query | BEFORE (ms) | AFTER (ms) | speed-up |\n|---|---|---|---|\n" +
               "\n".join(f"| {n} | {b:.1f} | {a:.3g} | {b / max(a, 0.001):.0f}x |" for n, b, a in summary) + "\n")
    create(*OPT_INDEXES)
    admin.execute("ANALYZE")
    dest = ROOT / "docs" / "performance" / "sql-before-after.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out), encoding="utf-8")
    print("wrote", dest)


if __name__ == "__main__":
    main()
