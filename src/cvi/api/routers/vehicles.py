from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query

from cvi.api.common import decode_cursor, encode_cursor, jsonable
from cvi.api.context import AppContext, dep_ctx
from cvi.api.security import Principal, mask_location, require
from cvi.ml.model import load_latest
from cvi.storage.pg import tenant_tx

router = APIRouter(prefix="/api/v1/vehicles", tags=["vehicles"])

LIST_SQL = """
SELECT v.id, v.vin, v.status, f.name AS fleet, m.make, m.model, m.model_year, m.fuel_type,
       h.health_score, s.risk_score, p.failure_prob,
       (SELECT count(*) FROM alerts a WHERE a.vehicle_id = v.id AND a.status = 'open') AS open_alerts
FROM vehicles v
JOIN fleets f ON f.id = v.fleet_id
JOIN vehicle_models m ON m.id = v.model_id
LEFT JOIN vehicle_health h ON h.vehicle_id = v.id
LEFT JOIN vehicle_safety s ON s.vehicle_id = v.id
LEFT JOIN latest_maintenance_predictions p ON p.vehicle_id = v.id
WHERE v.tenant_id = %(tenant)s AND v.erased_at IS NULL
  AND (%(status)s::text IS NULL OR v.status = %(status)s)
  AND (%(fleet)s::bigint IS NULL OR v.fleet_id = %(fleet)s)
  AND (%(max_health)s::int IS NULL OR h.health_score <= %(max_health)s)
  AND (%(q)s::text IS NULL OR v.vin LIKE %(q)s || '%%')
  AND (%(after)s::bigint IS NULL OR v.id > %(after)s)
ORDER BY v.id
LIMIT %(limit)s
"""


@router.get("")
def list_vehicles(status: str | None = None, fleet_id: int | None = None, max_health: int | None = Query(None, ge=0, le=100),
                  q: str | None = Query(None, min_length=3, max_length=17, pattern="^[A-Za-z0-9]+$"),
                  limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
                  p: Principal = Depends(require("vehicles:read")), ctx: AppContext = Depends(dep_ctx)):
    """Keyset (cursor) pagination on the primary key: O(limit) regardless of how deep the client has paged."""
    cur = decode_cursor(cursor)
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rows = c.execute(LIST_SQL, {"tenant": p.tenant_id, "status": status, "fleet": fleet_id, "max_health": max_health,
                                    "q": q.upper() if q else None, "after": cur[0] if cur else None, "limit": limit + 1}).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": jsonable(rows), "next_cursor": encode_cursor(rows[-1]["id"]) if more else None}


@router.get("/{vehicle_id}")
def vehicle_detail(vehicle_id: int, p: Principal = Depends(require("vehicles:read")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        v = c.execute(
            "SELECT v.id, v.vin, v.status, v.commissioned_at, f.name AS fleet, m.make, m.model, m.model_year, m.fuel_type, m.vehicle_class, "
            "h.health_score, h.factors, h.signals, h.last_event_ts, s.risk_score, s.harsh_brake_30m, s.harsh_accel_30m, s.speeding_30m, "
            "s.idle_seconds_today "
            "FROM vehicles v JOIN fleets f ON f.id = v.fleet_id JOIN vehicle_models m ON m.id = v.model_id "
            "LEFT JOIN vehicle_health h ON h.vehicle_id = v.id LEFT JOIN vehicle_safety s ON s.vehicle_id = v.id "
            "WHERE v.id = %s AND v.tenant_id = %s AND v.erased_at IS NULL", [vehicle_id, p.tenant_id]).fetchone()
        if not v:
            raise HTTPException(404, "vehicle not found")  # also the answer for other tenants' vehicles (no existence leak)
        pred = c.execute("SELECT model_version, failure_prob, horizon_days, top_factors, predicted_at FROM latest_maintenance_predictions "
                         "WHERE vehicle_id = %s", [vehicle_id]).fetchone()
        driver = c.execute("SELECT d.id, d.display_name FROM vehicle_assignments a JOIN drivers d ON d.id = a.driver_id "
                           "WHERE a.vehicle_id = %s AND a.to_ts IS NULL AND d.erased_at IS NULL ORDER BY a.from_ts DESC LIMIT 1", [vehicle_id]).fetchone()
        alerts = c.execute("SELECT id, alert_type_code, severity, status, cause, recommended_action, confidence, event_ts FROM alerts "
                           "WHERE vehicle_id = %s ORDER BY created_at DESC LIMIT 10", [vehicle_id]).fetchall()
    live = ctx.state.get_state(v["vin"].strip()) if ctx.state.ping() else None
    if live:
        lat, lon = mask_location(p, float(live["lat"]), float(live["lon"]))
        live = {k: live.get(k) for k in ("ts", "speed_kmh", "rpm", "odometer_km", "fuel_level_pct", "coolant_temp_c", "oil_pressure_kpa",
                                          "battery_soc_pct", "battery_soh_pct", "battery_temp_c", "voltage_v", "dtc", "engine_on")} | {"lat": lat, "lon": lon}
    return {"vehicle": jsonable([v])[0], "prediction": jsonable([pred])[0] if pred else None, "driver": driver,
            "recent_alerts": jsonable(alerts), "live": live}


@router.get("/{vehicle_id}/telemetry")
def vehicle_telemetry(vehicle_id: int, minutes: int = Query(30, ge=1, le=1440), limit: int = Query(300, ge=1, le=1000),
                      p: Principal = Depends(require("telemetry:read")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        row = c.execute("SELECT vin FROM vehicles WHERE id = %s AND tenant_id = %s", [vehicle_id, p.tenant_id]).fetchone()
    if not row:
        raise HTTPException(404, "vehicle not found")
    since = int((dt.datetime.now(dt.UTC).timestamp() - minutes * 60) * 1000)
    docs = ctx.raw.recent(row["vin"].strip(), limit, since)
    for d in docs:
        d["lat"], d["lon"] = mask_location(p, d["lat"], d["lon"])
        d.pop("tenant", None)
    return {"vin": row["vin"].strip(), "count": len(docs), "events": docs[::-1]}


def _require_vehicle(c, p: Principal, vehicle_id: int) -> None:
    """Uniform object-level authorisation: a vehicle outside the caller's tenant behaves exactly like a missing one."""
    if not c.execute("SELECT 1 FROM vehicles WHERE id = %s AND tenant_id = %s AND erased_at IS NULL", [vehicle_id, p.tenant_id]).fetchone():
        raise HTTPException(404, "vehicle not found")


@router.get("/{vehicle_id}/trips")
def vehicle_trips(vehicle_id: int, limit: int = Query(20, ge=1, le=100), p: Principal = Depends(require("vehicles:read")),
                  ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        _require_vehicle(c, p, vehicle_id)
        rows = c.execute("SELECT id, trip_key, started_at, ended_at, distance_km, max_speed, harsh_events, idle_seconds FROM trips "
                         "WHERE vehicle_id = %s AND tenant_id = %s ORDER BY started_at DESC LIMIT %s", [vehicle_id, p.tenant_id, limit]).fetchall()
    return {"items": jsonable(rows)}


@router.get("/{vehicle_id}/maintenance")
def vehicle_maintenance(vehicle_id: int, p: Principal = Depends(require("vehicles:read")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        _require_vehicle(c, p, vehicle_id)
        rec = c.execute("SELECT performed_at, kind, cost_cents, odometer_km, notes FROM maintenance_records WHERE vehicle_id = %s AND tenant_id = %s "
                        "ORDER BY performed_at DESC LIMIT 20", [vehicle_id, p.tenant_id]).fetchall()
        pred = c.execute("SELECT model_version, failure_prob, horizon_days, top_factors, predicted_at FROM maintenance_predictions "
                         "WHERE vehicle_id = %s AND tenant_id = %s ORDER BY predicted_at DESC LIMIT 10", [vehicle_id, p.tenant_id]).fetchall()
    m = load_latest()
    return {"records": jsonable(rec), "predictions": jsonable(pred), "model_version": m.version if m else None}


@router.get("/{vehicle_id}/history")
def vehicle_history(vehicle_id: int, hours: int = Query(24, ge=1, le=720), p: Principal = Depends(require("telemetry:read")),
                    ctx: AppContext = Depends(dep_ctx)):
    """Warm/cold tier: hourly aggregates of this vehicle's signals computed by DuckDB over partitioned Parquet."""
    from cvi.lifecycle.history import vehicle_hourly_history

    with tenant_tx(ctx.pool, p.tenant_id) as c:
        row = c.execute("SELECT vin FROM vehicles WHERE id = %s AND tenant_id = %s", [vehicle_id, p.tenant_id]).fetchone()
    if not row:
        raise HTTPException(404, "vehicle not found")
    return {"vin": row["vin"].strip(), "points": vehicle_hourly_history(ctx.objects, p.tenant_slug, row["vin"].strip(), hours)}
