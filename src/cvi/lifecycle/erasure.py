"""GDPR / DPDP-style erasure ("right to be forgotten") across every storage tier.

Vehicle subject: removes the vehicle's telemetry from Redis (hot), MongoDB (hot), Parquet (warm+cold; files are
rewritten without the vehicle) and pseudonymises/removes the relational footprint.  Driver subject: the driver's
identity is anonymised (name replaced, licence hash dropped) while aggregate/safety statistics remain (no personal data).
A report of what was deleted is stored in erasure_requests and the action is written to the tamper-evident audit log."""

from __future__ import annotations

import io
import logging

import pyarrow.parquet as pq
from psycopg_pool import ConnectionPool

from cvi.storage.mongo_store import RawEventStore
from cvi.storage.object_store import ObjectStore
from cvi.storage.redis_store import StateStore

logger = logging.getLogger("erasure")


def _scrub_parquet(store: ObjectStore, tenant: str, vin: str) -> dict:
    files = rewritten = rows_removed = 0
    for key in store.list(f"telemetry/tier=warm/tenant={tenant}/") + store.list(f"telemetry/tier=cold/tenant={tenant}/"):
        files += 1
        tbl = pq.read_table(io.BytesIO(store.get(key)))
        mask = [v != vin for v in tbl["vin"].to_pylist()]
        removed = len(mask) - sum(mask)
        if not removed:
            continue
        rows_removed += removed
        rewritten += 1
        kept = tbl.filter(mask)
        if kept.num_rows == 0:
            store.delete(key)
        else:
            buf = io.BytesIO()
            pq.write_table(kept, buf, compression="zstd")
            store.put(key, buf.getvalue())  # atomic replace
    return {"files_scanned": files, "files_rewritten": rewritten, "rows_removed": rows_removed}


def erase_vehicle(pool: ConnectionPool, state: StateStore, raw: RawEventStore, objects: ObjectStore, tenant_id: int, tenant_slug: str,
                  vin: str) -> dict:
    report: dict = {}
    with pool.connection() as c, c.transaction():
        c.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
        row = c.execute("SELECT id FROM vehicles WHERE vin = %s AND tenant_id = %s", [vin, tenant_id]).fetchone()
        if not row:
            raise LookupError("vehicle not found in this tenant")
        vid = row["id"]
        report["alerts"] = c.execute("DELETE FROM alerts WHERE vehicle_id = %s", [vid]).rowcount  # cascades incident embeddings
        for t in ("vehicle_health", "vehicle_safety"):
            report[t] = c.execute(f"DELETE FROM {t} WHERE vehicle_id = %s", [vid]).rowcount  # noqa: S608  # nosec B608 - table names are constants
        for t in ("trips", "maintenance_records", "maintenance_predictions", "vehicle_assignments"):
            report[t] = c.execute(f"DELETE FROM {t} WHERE vehicle_id = %s", [vid]).rowcount  # noqa: S608  # nosec B608 - table names are constants
        report["daily_vehicle_stats"] = c.execute("DELETE FROM daily_vehicle_stats WHERE vehicle_id = %s", [vid]).rowcount
        # keep a tombstone row so the VIN is rejected by ingestion (events for erased vehicles go to the DLQ)
        c.execute("UPDATE vehicles SET erased_at = now(), status = 'retired' WHERE id = %s", [vid])
    report["redis_keys"] = state.delete_vehicle(vin)
    report["mongo_documents"] = raw.delete_vehicle(vin)
    report["parquet"] = _scrub_parquet(objects, tenant_slug, vin)
    return report


def erase_driver(pool: ConnectionPool, tenant_id: int, driver_id: int) -> dict:
    with pool.connection() as c, c.transaction():
        c.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
        n = c.execute("UPDATE drivers SET display_name = 'erased-driver-' || id, licence_hash = '', erased_at = now() "
                      "WHERE id = %s AND tenant_id = %s AND erased_at IS NULL", [driver_id, tenant_id]).rowcount
        if not n:
            raise LookupError("driver not found or already erased")
        ended = c.execute("UPDATE vehicle_assignments SET to_ts = coalesce(to_ts, now()) WHERE driver_id = %s", [driver_id]).rowcount
    return {"driver_anonymised": n, "assignments_closed": ended}
