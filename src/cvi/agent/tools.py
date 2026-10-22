"""Agent tools: every one reads real application data through the same tenant-scoped, RLS-protected paths as the API.

Each tool declares: required permission, a validated input model, and whether it is destructive (needs confirmation)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from cvi.algorithms.geo import geohash_encode, haversine_km
from cvi.algorithms.graph import astar, grid_road_graph, knapsack_schedule
from cvi.api.common import jsonable
from cvi.api.context import AppContext
from cvi.api.security import Principal, mask_location
from cvi.common.config import get_settings
from cvi.ml.model import load_latest
from cvi.storage.pg import tenant_tx
from cvi.storage.vector import search_knowledge, similar_incidents

VIN = Field(pattern=r"^[A-HJ-NPR-Z0-9]{17}$")


class NoArgs(BaseModel):
    pass


class VinArgs(BaseModel):
    vin: str = VIN


class RecentAlertsArgs(BaseModel):
    severity: str | None = Field(default=None, pattern="^(info|warning|critical)$")
    alert_type: str | None = Field(default=None, pattern="^[a-z_]{3,40}$")
    vin: str | None = Field(default=None, pattern=r"^[A-HJ-NPR-Z0-9]{17}$")
    limit: int = Field(default=10, ge=1, le=50)


class LimitArgs(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)


class MaintArgs(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)
    min_prob: float = Field(default=0.3, ge=0, le=1)


class SearchTelemetryArgs(BaseModel):
    vin: str = VIN
    minutes: int = Field(default=30, ge=1, le=240)


class IdleArgs(BaseModel):
    days: int = Field(default=7, ge=1, le=90)
    reduction_pct: float = Field(default=30, ge=0, le=100)


class NearbyArgs(BaseModel):
    vin: str | None = VIN
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    k: int = Field(default=3, ge=1, le=10)


class KnowledgeArgs(BaseModel):
    query: str = Field(min_length=3, max_length=300)


class PlanArgs(BaseModel):
    capacity_hours: int = Field(default=24, ge=1, le=200)


class CreateAlertArgs(BaseModel):
    vin: str = VIN
    title: str = Field(min_length=3, max_length=120)
    severity: str = Field(default="warning", pattern="^(info|warning|critical)$")


class AckAlertArgs(BaseModel):
    alert_id: int = Field(ge=1)


@dataclass
class Tool:
    name: str
    description: str
    permission: str
    args: type[BaseModel]
    fn: Callable[[AppContext, Principal, Any], dict]
    destructive: bool = False


def _q(ctx: AppContext, p: Principal, sql: str, params: list | dict | None = None) -> list[dict]:
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        return c.execute(sql, params).fetchall()


def _vehicle_by_vin(ctx: AppContext, p: Principal, vin: str) -> dict | None:
    rows = _q(ctx, p, "SELECT v.id, v.vin, v.status, m.make, m.model, m.model_year, m.fuel_type, f.name AS fleet FROM vehicles v "
                      "JOIN vehicle_models m ON m.id = v.model_id JOIN fleets f ON f.id = v.fleet_id "
                      "WHERE v.vin = %s AND v.tenant_id = %s AND v.erased_at IS NULL", [vin, p.tenant_id])
    return rows[0] if rows else None


# ----------------------------------------------------------------------------------------------- read tools
def get_fleet_summary(ctx, p, a):
    from cvi.api.routers.analytics import summary

    return {"summary": summary(p, ctx)}


def get_vehicle(ctx, p, a):
    v = _vehicle_by_vin(ctx, p, a.vin)
    if not v:
        return {"found": False, "message": f"No vehicle {a.vin} in your fleet."}
    live = ctx.state.get_state(a.vin)
    out = {"found": True, "vehicle": jsonable([v])[0]}
    if live:
        lat, lon = mask_location(p, float(live["lat"]), float(live["lon"]))
        out["live"] = {"speed_kmh": float(live["speed_kmh"]), "lat": lat, "lon": lon, "ts": int(float(live["ts"])), "dtc": live.get("dtc", "")}
    return out


def get_vehicle_health(ctx, p, a):
    v = _vehicle_by_vin(ctx, p, a.vin)
    if not v:
        return {"found": False, "message": f"No vehicle {a.vin} in your fleet."}
    rows = _q(ctx, p, "SELECT h.health_score, h.factors, h.signals, h.last_event_ts, s.risk_score, s.harsh_brake_30m, s.harsh_accel_30m, "
                      "s.speeding_30m, s.idle_seconds_today FROM vehicles v LEFT JOIN vehicle_health h ON h.vehicle_id = v.id "
                      "LEFT JOIN vehicle_safety s ON s.vehicle_id = v.id WHERE v.id = %s", [v["id"]])
    pred = _q(ctx, p, "SELECT failure_prob, top_factors, model_version FROM latest_maintenance_predictions WHERE vehicle_id = %s", [v["id"]])
    alerts = _q(ctx, p, "SELECT id, alert_type_code AS type, severity, cause, recommended_action, event_ts FROM alerts "
                        "WHERE vehicle_id = %s AND status = 'open' ORDER BY created_at DESC LIMIT 5", [v["id"]])
    return {"found": True, "vin": a.vin, "vehicle": jsonable([v])[0], "health": jsonable(rows)[0] if rows else None,
            "prediction": jsonable(pred)[0] if pred else None, "open_alerts": jsonable(alerts)}


def get_recent_alerts(ctx, p, a):
    where, params = ["a.tenant_id = %s", "a.status = 'open'"], [p.tenant_id]
    if a.severity:
        where.append("a.severity = %s")
        params.append(a.severity)
    if a.alert_type:
        where.append("a.alert_type_code = %s")
        params.append(a.alert_type)
    if a.vin:
        where.append("v.vin = %s")
        params.append(a.vin)
    params.append(a.limit)
    rows = _q(ctx, p, "SELECT a.id, v.vin, a.alert_type_code AS type, a.severity, a.confidence, a.cause, a.recommended_action, a.event_ts, a.details "
                      f"FROM alerts a JOIN vehicles v ON v.id = a.vehicle_id WHERE {' AND '.join(where)} "  # noqa: S608  # nosec B608 - fixed fragments, values bound
                      "ORDER BY a.created_at DESC, a.id DESC LIMIT %s", params)
    return {"alerts": jsonable(rows), "count": len(rows)}


def search_telemetry(ctx, p, a):
    v = _vehicle_by_vin(ctx, p, a.vin)
    if not v:
        return {"found": False, "message": f"No vehicle {a.vin} in your fleet."}
    import time

    docs = ctx.raw.recent(a.vin, 600, int((time.time() - a.minutes * 60) * 1000))
    stats: dict[str, dict] = {}
    for sig in ("speed_kmh", "coolant_temp_c", "oil_pressure_kpa", "rpm", "battery_soc_pct", "battery_soh_pct", "battery_temp_c", "voltage_v"):
        vals = [d[sig] for d in docs if d.get(sig) is not None]
        if vals:
            stats[sig] = {"min": round(min(vals), 1), "max": round(max(vals), 1), "avg": round(sum(vals) / len(vals), 1), "n": len(vals)}
    dtcs: dict[str, int] = {}
    for d in docs:
        for c in d.get("dtc") or []:
            dtcs[c] = dtcs.get(c, 0) + 1
    return {"found": True, "vin": a.vin, "window_minutes": a.minutes, "samples": len(docs), "signals": stats, "dtc_counts": dtcs,
            "harsh_brakes": sum(1 for d in docs if d.get("harsh_brake")), "speeding_samples": sum(1 for d in docs if d.get("speeding"))}


def get_driver_risk(ctx, p, a):
    rows = _q(ctx, p, "SELECT v.vin, s.risk_score, s.harsh_brake_30m, s.harsh_accel_30m, s.speeding_30m, "
                      "(SELECT d.display_name FROM vehicle_assignments va JOIN drivers d ON d.id = va.driver_id WHERE va.vehicle_id = v.id "
                      " AND va.to_ts IS NULL AND d.erased_at IS NULL LIMIT 1) AS driver "
                      "FROM vehicle_safety s JOIN vehicles v ON v.id = s.vehicle_id WHERE s.tenant_id = %s ORDER BY s.risk_score DESC LIMIT %s",
              [p.tenant_id, a.limit])
    return {"drivers": jsonable(rows)}


def get_maintenance_predictions(ctx, p, a):
    rows = _q(ctx, p, "SELECT v.vin, m.make, m.model, pr.failure_prob, pr.top_factors, pr.model_version FROM latest_maintenance_predictions pr "
                      "JOIN vehicles v ON v.id = pr.vehicle_id JOIN vehicle_models m ON m.id = v.model_id "
                      "WHERE pr.tenant_id = %s AND pr.failure_prob >= %s ORDER BY pr.failure_prob DESC LIMIT %s", [p.tenant_id, a.min_prob, a.limit])
    model = load_latest()
    return {"predictions": jsonable(rows), "model_version": model.version if model else None,
            "note": "Predictions come from a model trained on SYNTHETIC data (see ml/artifacts/*/metrics.json)."}


def calculate_idle_cost(ctx, p, a):
    s = get_settings()
    r = _q(ctx, p, "SELECT coalesce(sum(idle_seconds), 0) AS idle_s, count(DISTINCT vehicle_id) FILTER (WHERE idle_seconds > 0) AS vehicles "
                   "FROM daily_vehicle_stats WHERE tenant_id = %s AND day >= current_date - %s", [p.tenant_id, a.days - 1])[0]
    hours = float(r["idle_s"]) / 3600
    litres = hours * s.idle_fuel_lph
    cost = litres * s.fuel_price_per_litre
    saved = cost * a.reduction_pct / 100
    return {"days": a.days, "idle_hours": round(hours, 1), "idling_vehicles": r["vehicles"], "fuel_litres": round(litres, 1),
            "cost_usd": round(cost, 2), "reduction_pct": a.reduction_pct, "potential_saving_usd": round(saved, 2),
            "annualised_saving_usd": round(saved * 365 / a.days, 2),
            "assumptions": {"idle_fuel_lph": s.idle_fuel_lph, "fuel_price_per_litre": s.fuel_price_per_litre}}


def get_utilization_analysis(ctx, p, a):
    rows = _q(ctx, p, "SELECT day, active_vehicles, distance_km, drive_seconds, idle_seconds FROM mv_fleet_daily_kpis WHERE tenant_id = %s "
                      "AND day >= current_date - 8 ORDER BY day", [p.tenant_id])
    total = _q(ctx, p, "SELECT count(*) AS n, count(*) FILTER (WHERE status = 'maintenance') AS in_maint FROM vehicles WHERE tenant_id = %s "
                       "AND erased_at IS NULL", [p.tenant_id])[0]
    crit = _q(ctx, p, "SELECT count(DISTINCT vehicle_id) AS n FROM alerts WHERE tenant_id = %s AND status = 'open' AND severity = 'critical'", [p.tenant_id])[0]
    return {"daily": jsonable(rows), "total_vehicles": total["n"], "in_maintenance": total["in_maint"], "vehicles_with_critical_alerts": crit["n"]}


_GRIDS: dict[str, tuple] = {}


def _grid_for(tenant_slug: str) -> tuple:
    """Synthetic 61x61 street grid (1.1 km blocks) centred on the tenant's depot. Replace with a real road graph in production."""
    if tenant_slug not in _GRIDS:
        from cvi.synthetic.identity import TENANTS

        t = next((t for t in TENANTS if t.slug == tenant_slug), TENANTS[0])
        lat0, lon0 = t.home_lat - 0.3, t.home_lon - 0.3
        g, c = grid_road_graph(61, lat0, lon0, 0.01)
        _GRIDS[tenant_slug] = (g, c, lat0, lon0)
    return _GRIDS[tenant_slug]


def find_nearby_vehicle(ctx, p, a):
    """k nearest vehicles via geohash cell lookup in Redis (+ neighbours), ETA by A* over a synthetic street grid."""
    if a.vin:
        st = ctx.state.get_state(a.vin)
        if not st or st.get("tenant") != p.tenant_slug:
            return {"found": False, "message": "No live position for that vehicle."}
        lat, lon = float(st["lat"]), float(st["lon"])
    elif a.lat is not None and a.lon is not None:
        lat, lon = a.lat, a.lon
    else:
        return {"found": False, "message": "Provide a vin or lat/lon."}
    from cvi.algorithms.geo import geohash_neighbors
    from cvi.storage.redis_store import CELL_PRECISION

    gh = geohash_encode(lat, lon, CELL_PRECISION)
    vins = ctx.state.vins_in_cells(p.tenant_slug, [gh, *geohash_neighbors(gh)], 3000)
    cands = []
    for st in ctx.state.get_states([v for v in vins if v != a.vin]):
        d = haversine_km(lat, lon, float(st["lat"]), float(st["lon"]))
        cands.append((d, st))
    cands.sort(key=lambda x: x[0])
    graph, coords, lat0, lon0 = _grid_for(p.tenant_slug)
    n = 61

    def snap(la: float, lo: float) -> str:
        i = min(n - 1, max(0, round((la - lat0) / 0.01)))
        j = min(n - 1, max(0, round((lo - lon0) / 0.01)))
        return f"{i}:{j}"

    out = []
    for d, st in cands[: a.k]:
        s_, t_ = snap(lat, lon), snap(float(st["lat"]), float(st["lon"]))
        dist, _path = astar(graph, s_, t_, lambda n_, t__=t_: haversine_km(*coords[n_], *coords[t__]))
        road_km = dist if math.isfinite(dist) and dist > 0 else d * 1.3
        mlat, mlon = mask_location(p, float(st["lat"]), float(st["lon"]))
        out.append({"vin": st["vin"], "straight_line_km": round(d, 2), "est_road_km": round(max(road_km, d), 2),
                    "eta_min": round(max(road_km, d) / 35.0 * 60, 1), "status": "moving" if float(st["speed_kmh"]) > 3 else "stationary",
                    "health": int(float(st.get("health", 100) or 100)), "lat": mlat, "lon": mlon})
    return {"found": bool(out), "origin_geohash": gh, "vehicles": out}


def search_knowledge_base(ctx, p, a):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        docs = search_knowledge(c, a.query, 3)
        incidents = similar_incidents(c, p.tenant_id, a.query, 3)
    return {"documents": [{"title": d["title"], "category": d["category"], "content": d["content"], "score": round(float(d["score"]), 3)} for d in docs],
            "similar_incidents": [{"alert_id": i["alert_id"], "summary": i["summary"], "score": round(float(i["score"]), 3)} for i in incidents]}


def recommend_maintenance_plan(ctx, p, a):
    """0/1 knapsack (dynamic programming): maximise total failure risk removed within the workshop's weekly hours."""
    rows = _q(ctx, p, "SELECT v.vin, m.fuel_type, pr.failure_prob FROM latest_maintenance_predictions pr JOIN vehicles v ON v.id = pr.vehicle_id "
                      "JOIN vehicle_models m ON m.id = v.model_id WHERE pr.tenant_id = %s AND pr.failure_prob >= 0.2 "
                      "ORDER BY pr.failure_prob DESC LIMIT 60", [p.tenant_id])
    items = [(r["vin"], 6 if r["fuel_type"] == "ev" else 4, float(r["failure_prob"])) for r in rows]
    value, chosen = knapsack_schedule(items, a.capacity_hours)
    hours = {i[0]: i[1] for i in items}
    return {"capacity_hours": a.capacity_hours, "candidates": len(items), "scheduled": [{"vin": v, "hours": hours[v],
            "failure_prob": next(float(r["failure_prob"]) for r in rows if r["vin"] == v)} for v in chosen],
            "total_hours": sum(hours[v] for v in chosen), "expected_failures_avoided": round(value, 2), "method": "0/1 knapsack DP"}


# ----------------------------------------------------------------------------------------------- write tools (confirmation)
def create_alert(ctx, p, a):
    v = _vehicle_by_vin(ctx, p, a.vin)
    if not v:
        return {"created": False, "message": "vehicle not found"}
    import time

    key = f"manual:{a.vin}:{int(time.time())}"
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        c.execute("INSERT INTO alert_types (code, category, title, default_severity) VALUES ('manual', 'fleet', 'Manual alert', 'warning') ON CONFLICT DO NOTHING")
        row = c.execute("INSERT INTO alerts (tenant_id, vehicle_id, alert_type_code, severity, confidence, cause, recommended_action, event_ts, dedup_key) "
                        "VALUES (%s,%s,'manual',%s,1,%s,'Follow up with the vehicle owner as noted.', now(), %s) RETURNING id",
                        [p.tenant_id, v["id"], a.severity, f"Created via Fleet Copilot: {a.title}", key]).fetchone()
    return {"created": True, "alert_id": row["id"]}


def acknowledge_alert(ctx, p, a):
    rows = _q(ctx, p, "UPDATE alerts SET status = 'acknowledged', acknowledged_by = %s WHERE id = %s AND tenant_id = %s RETURNING id",
              [p.user_id, a.alert_id, p.tenant_id])
    return {"acknowledged": bool(rows), "alert_id": a.alert_id}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_fleet_summary", "KPIs for the whole fleet: vehicles, alerts, health, safety, utilization, idling.", "analytics:read", NoArgs, get_fleet_summary),
    Tool("get_vehicle", "Static + live details for one vehicle by VIN.", "vehicles:read", VinArgs, get_vehicle),
    Tool("get_vehicle_health", "Health score, factors, safety risk, maintenance prediction and open alerts for a VIN.", "vehicles:read", VinArgs, get_vehicle_health),
    Tool("get_recent_alerts", "Open alerts, optionally filtered by severity/type/VIN.", "alerts:read", RecentAlertsArgs, get_recent_alerts),
    Tool("search_telemetry", "Signal statistics and DTCs for a VIN over the last N minutes (raw telemetry).", "telemetry:read", SearchTelemetryArgs, search_telemetry),
    Tool("get_driver_risk", "Highest driver/vehicle risk scores.", "analytics:read", LimitArgs, get_driver_risk),
    Tool("get_maintenance_predictions", "Vehicles with the highest predicted failure probability (ML).", "analytics:read", MaintArgs, get_maintenance_predictions),
    Tool("calculate_idle_cost", "Fuel and money wasted idling, and savings from reducing it.", "analytics:read", IdleArgs, calculate_idle_cost),
    Tool("get_utilization_analysis", "Recent daily utilization vs maintenance/critical-alert context.", "analytics:read", NoArgs, get_utilization_analysis),
    Tool("find_nearby_vehicle", "Closest vehicles to a vehicle or coordinates with ETA (geohash + A*).", "vehicles:read", NearbyArgs, find_nearby_vehicle),
    Tool("search_knowledge_base", "Semantic search over maintenance knowledge and similar past incidents (pgvector).", "vehicles:read", KnowledgeArgs, search_knowledge_base),
    Tool("recommend_maintenance_plan", "Choose which vehicles to service within the workshop's hours (knapsack DP).", "analytics:read", PlanArgs, recommend_maintenance_plan),
    Tool("create_alert", "Create a manual alert for a vehicle (WRITE: requires confirmation).", "agent:act", CreateAlertArgs, create_alert, destructive=True),
    Tool("acknowledge_alert", "Acknowledge an alert (WRITE: requires confirmation).", "agent:act", AckAlertArgs, acknowledge_alert, destructive=True),
]}
