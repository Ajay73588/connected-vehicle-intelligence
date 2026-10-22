"""Data-lifecycle jobs (HOT -> WARM -> COLD -> deleted) and scheduled batch scoring.

  HOT   Redis latest state/aggregates (hours-days, TTL)  +  MongoDB raw events (TTL index, 7 d)
  WARM  Parquet `tier=warm/.../hour=HH/part-*.parquet` (small hourly files, zstd)        retained 30 d
  COLD  Parquet `tier=cold/.../date=D/day.parquet` (one compacted, sorted file per day)  retained 730 d
  Compaction: hourly warm files older than 24 h are merged into one daily cold file (idempotent: same input -> same
  output), de-duplicated by event_id, then the warm files are deleted. Cloud lifecycle rules (Terraform) additionally
  move the cold prefix to an infrequent-access storage class."""

from __future__ import annotations

import io
import logging
import time

import pyarrow.compute as pc
import pyarrow.parquet as pq

from cvi.storage.object_store import ObjectStore, read_table

logger = logging.getLogger("lifecycle")


def _date_of(key: str) -> str:
    return key.split("date=")[1].split("/")[0]


def compact_day(store: ObjectStore, tenant: str, date: str) -> dict:
    keys = store.list(f"telemetry/tier=warm/tenant={tenant}/date={date}/")
    if not keys:
        return {"tenant": tenant, "date": date, "files_in": 0, "rows": 0}
    tbl = read_table(store, keys)
    # drop duplicate event ids (replays / at-least-once writers) and sort by vehicle,time for tight row-group stats
    ids = tbl["event_id"].to_pylist()
    seen, mask = set(), []
    for i in ids:
        mask.append(i not in seen)
        seen.add(i)
    tbl = tbl.filter(mask)
    tbl = tbl.sort_by([("vin", "ascending"), ("ts", "ascending")])
    buf = io.BytesIO()
    pq.write_table(tbl, buf, compression="zstd", row_group_size=100_000)
    cold_key = f"telemetry/tier=cold/tenant={tenant}/date={date}/day.parquet"
    store.put(cold_key, buf.getvalue())
    for k in keys:  # delete warm files only after the cold object is durable
        store.delete(k)
    return {"tenant": tenant, "date": date, "files_in": len(keys), "rows": tbl.num_rows, "cold_key": cold_key}


def run_compaction(store: ObjectStore, older_than_hours: int = 24) -> list[dict]:
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - older_than_hours * 3600))
    done, seen = [], set()
    for k in store.list("telemetry/tier=warm/"):
        tenant = k.split("tenant=")[1].split("/")[0]
        date = _date_of(k)
        if date <= cutoff and (tenant, date) not in seen:
            seen.add((tenant, date))
            done.append(compact_day(store, tenant, date))
    return done


def expire_tiers(store: ObjectStore, warm_days: int = 30, cold_days: int = 730) -> dict:
    now = time.time()
    removed = {"warm": 0, "cold": 0}
    for tier, days in (("warm", warm_days), ("cold", cold_days)):
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - days * 86400))
        for k in store.list(f"telemetry/tier={tier}/"):
            if _date_of(k) < cutoff:
                store.delete(k)
                removed[tier] += 1
    return removed


def refresh_kpis(pool) -> None:
    from psycopg import connect

    # REFRESH ... CONCURRENTLY cannot run inside a transaction block
    with connect(pool.conninfo, autocommit=True) as c:
        c.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_fleet_daily_kpis")


def prune_old_predictions(pool, keep_days: int = 30) -> int:
    with pool.connection() as c, c.transaction():
        return c.execute("DELETE FROM maintenance_predictions WHERE predicted_at < now() - make_interval(days => %s)", [keep_days]).rowcount


_unused = pc  # pyarrow.compute is imported for downstream filters
