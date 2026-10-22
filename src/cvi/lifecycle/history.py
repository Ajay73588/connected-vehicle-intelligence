"""Historical (warm/cold tier) analytics: DuckDB over partitioned Parquet in object storage."""

from __future__ import annotations

import time

import duckdb

from cvi.storage.object_store import ObjectStore, read_table


def tenant_keys(store: ObjectStore, tenant: str, hours: int) -> list[str]:
    """Partition pruning by path: only date folders overlapping the requested range are listed/read."""
    now = time.time()
    days = sorted({time.strftime("%Y-%m-%d", time.gmtime(now - h * 3600)) for h in range(0, hours + 1, 6)} | {time.strftime("%Y-%m-%d", time.gmtime(now))})
    keys: list[str] = []
    for tier in ("warm", "cold"):
        for d in days:
            keys += store.list(f"telemetry/tier={tier}/tenant={tenant}/date={d}/")
    return keys


def vehicle_hourly_history(store: ObjectStore, tenant: str, vin: str, hours: int = 24) -> list[dict]:
    keys = tenant_keys(store, tenant, hours)
    if not keys:
        return []
    table = read_table(store, keys)
    cutoff = int((time.time() - hours * 3600) * 1000)
    con = duckdb.connect()
    con.register("t", table)
    rows = con.execute(
        """
        SELECT date_trunc('hour', to_timestamp(ts / 1000)) AS hour, count(*) AS samples, round(avg(speed_kmh), 1) AS avg_speed,
               round(max(coolant_temp_c), 1) AS max_coolant, round(min(battery_soh_pct), 2) AS min_soh,
               sum(CASE WHEN harsh_brake THEN 1 ELSE 0 END) AS harsh_brakes, max(odometer_km) - min(odometer_km) AS km
        FROM (SELECT * FROM t WHERE vin = ? AND ts >= ?
              QUALIFY row_number() OVER (PARTITION BY event_id) = 1)   -- replay-safe: drop duplicate event ids
        GROUP BY 1 ORDER BY 1
        """, [vin, cutoff]).fetchall()
    return [{"hour": r[0].isoformat(), "samples": r[1], "avg_speed": r[2], "max_coolant": r[3], "min_soh": r[4],
             "harsh_brakes": r[5], "km": round(r[6] or 0, 2)} for r in rows]


def tenant_daily_summary(store: ObjectStore, tenant: str, days: int = 7) -> list[dict]:
    keys = tenant_keys(store, tenant, days * 24)
    if not keys:
        return []
    con = duckdb.connect()
    con.register("t", read_table(store, keys))
    rows = con.execute(
        """SELECT CAST(to_timestamp(ts / 1000) AS DATE) AS day, count(DISTINCT event_id) AS events, count(DISTINCT vin) AS vehicles,
                  sum(CASE WHEN speeding THEN 1 ELSE 0 END) AS speeding_samples
           FROM t GROUP BY 1 ORDER BY 1""").fetchall()
    return [{"day": str(r[0]), "events": r[1], "vehicles": r[2], "speeding_samples": r[3]} for r in rows]
