from __future__ import annotations

import functools
import time

from fastapi import APIRouter, Depends, Query

from cvi.algorithms.sketches import top_k_exact
from cvi.api.common import jsonable
from cvi.api.context import AppContext, dep_ctx
from cvi.api.security import Principal, require
from cvi.common.config import get_settings
from cvi.storage.pg import tenant_tx

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


def cached(name: str, ttl: float):
    """Cache an endpoint's response per tenant+parameters in Redis for `ttl` seconds (stampede-protected). If Redis is
    unavailable the endpoint computes directly (graceful degradation)."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            p, ctx = kwargs["p"], kwargs["ctx"]
            key = f"{name}:{p.tenant_id}:" + ":".join(f"{k}={v}" for k, v in sorted(kwargs.items()) if k not in ("p", "ctx"))
            try:
                return ctx.state.cached(key, ttl, lambda: fn(*args, **kwargs))
            except (ConnectionError, TimeoutError, OSError):
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - redis.RedisError and friends
                if type(e).__module__.startswith("redis"):
                    return fn(*args, **kwargs)
                raise

        return wrapper

    return deco


def _hours(n: int) -> list[str]:
    now = time.time()
    return [time.strftime("%Y%m%d%H", time.gmtime(now - i * 3600)) for i in range(n - 1, -1, -1)]


@router.get("/summary")
def summary(p: Principal = Depends(require("analytics:read")), ctx: AppContext = Depends(dep_ctx)):
    """Executive dashboard. Cached for 2 s in Redis (stampede-protected) so N dashboards cost 1 query set per 2 s."""
    def compute():
        s = get_settings()
        with tenant_tx(ctx.pool, p.tenant_id) as c:
            tot = c.execute("SELECT count(*) AS n FROM vehicles WHERE tenant_id = %s AND status <> 'retired' AND erased_at IS NULL",
                            [p.tenant_id]).fetchone()["n"]
            alerts = {r["severity"]: r["n"] for r in c.execute(
                "SELECT severity, count(*) AS n FROM alerts WHERE tenant_id = %s AND status = 'open' GROUP BY severity", [p.tenant_id])}
            health = c.execute("SELECT round(avg(health_score), 1) AS avg, count(*) FILTER (WHERE health_score < 60) AS poor "
                               "FROM vehicle_health WHERE tenant_id = %s", [p.tenant_id]).fetchone()
            safety = c.execute("SELECT round(avg(risk_score), 1) AS avg_risk, count(*) FILTER (WHERE risk_score >= 70) AS risky "
                               "FROM vehicle_safety WHERE tenant_id = %s", [p.tenant_id]).fetchone()
            risk = c.execute("SELECT count(*) AS n FROM latest_maintenance_predictions WHERE tenant_id = %s AND failure_prob >= 0.5",
                             [p.tenant_id]).fetchone()["n"]
            today = c.execute("SELECT count(*) FILTER (WHERE distance_km > 0.5) AS moving, coalesce(sum(distance_km), 0) AS km, "
                              "coalesce(sum(drive_seconds), 0) AS drive_s, coalesce(sum(idle_seconds), 0) AS idle_s, "
                              "coalesce(sum(fuel_litres), 0) AS fuel_l, coalesce(sum(energy_kwh), 0) AS kwh "
                              "FROM daily_vehicle_stats WHERE tenant_id = %s AND day = (now() AT TIME ZONE 'utc')::date", [p.tenant_id]).fetchone()
        live_active = ctx.state.active_count(p.tenant_slug)
        hours = ctx.state.hourly_aggregates(p.tenant_slug, _hours(1))
        cur = next(iter(hours.values()), {})
        idle_l = float(today["idle_s"]) / 3600 * s.idle_fuel_lph
        return {
            "total_vehicles": tot, "active_vehicles": live_active,
            "open_alerts": {"critical": alerts.get("critical", 0), "warning": alerts.get("warning", 0), "info": alerts.get("info", 0),
                            "total": sum(alerts.values())},
            "fleet_health": {"avg_score": float(health["avg"] or 0), "vehicles_below_60": health["poor"]},
            "safety": {"score": round(100 - float(safety["avg_risk"] or 0), 1), "high_risk_vehicles": safety["risky"]},
            "maintenance_risk": {"vehicles_over_50pct": risk},
            "utilization": {"vehicles_moving_today": today["moving"], "pct": round(100 * today["moving"] / tot, 1) if tot else 0,
                            "distance_km": round(float(today["km"]), 1), "drive_hours": round(float(today["drive_s"]) / 3600, 1)},
            "energy": {"fuel_litres": round(float(today["fuel_l"]), 1), "energy_kwh": round(float(today["kwh"]), 1)},
            "idling": {"hours": round(float(today["idle_s"]) / 3600, 2), "est_fuel_litres": round(idle_l, 1),
                       "est_cost_usd": round(idle_l * s.fuel_price_per_litre, 2)},
            "realtime": {"events_this_hour": int(cur.get("n", 0)), "harsh_events_this_hour": int(cur.get("hb", 0) + cur.get("ha", 0)),
                         "speeding_this_hour": int(cur.get("sp", 0))},
            "generated_at": time.time(),
        }
    return ctx.state.cached(f"summary:{p.tenant_id}", 2.0, compute)


@router.get("/trends")
@cached("trends", 30)
def trends(days: int = Query(14, ge=1, le=90), p: Principal = Depends(require("analytics:read")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rows = c.execute("SELECT day, active_vehicles, round(distance_km::numeric, 1) AS distance_km, round((drive_seconds / 3600.0)::numeric, 1) AS drive_hours, "
                         "round((idle_seconds / 3600.0)::numeric, 1) AS idle_hours, round(fuel_litres::numeric, 1) AS fuel_litres, "
                         "round(energy_kwh::numeric, 1) AS energy_kwh, safety_events FROM mv_fleet_daily_kpis "
                         "WHERE tenant_id = %s AND day >= current_date - %s ORDER BY day", [p.tenant_id, days]).fetchall()
    return {"items": jsonable(rows)}


@router.get("/live")
def live(hours: int = Query(12, ge=1, le=48), p: Principal = Depends(require("analytics:read")), ctx: AppContext = Depends(dep_ctx)):
    hs = _hours(hours)
    agg = ctx.state.hourly_aggregates(p.tenant_slug, hs)
    return {"items": [{"hour": h, "events": int(a.get("n", 0)), "active_vehicles": int(a["active_vehicles"]),
                       "harsh": int(a.get("hb", 0) + a.get("ha", 0)), "speeding": int(a.get("sp", 0)),
                       "late": int(a.get("late", 0))} for h, a in agg.items()]}


@router.get("/safety")
@cached("safety", 10)
def safety(limit: int = Query(10, ge=1, le=50), p: Principal = Depends(require("analytics:read")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rows = c.execute("SELECT v.id, v.vin, s.risk_score, s.harsh_brake_30m, s.harsh_accel_30m, s.speeding_30m "
                         "FROM vehicle_safety s JOIN vehicles v ON v.id = s.vehicle_id WHERE s.tenant_id = %s "
                         "ORDER BY s.risk_score DESC LIMIT %s", [p.tenant_id, limit]).fetchall()
        dist = c.execute("SELECT width_bucket(risk_score, 0, 100, 5) AS bucket, count(*) AS n FROM vehicle_safety WHERE tenant_id = %s "
                         "GROUP BY 1 ORDER BY 1", [p.tenant_id]).fetchall()
    return {"top_risky": jsonable(rows), "distribution": jsonable(dist)}


@router.get("/battery")
@cached("battery", 30)
def battery(p: Principal = Depends(require("analytics:read")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        dist = c.execute("SELECT width_bucket((h.signals->>'soh')::numeric, 60, 100, 8) AS bucket, count(*) AS n FROM vehicle_health h "
                         "WHERE h.tenant_id = %s AND h.signals ? 'soh' GROUP BY 1 ORDER BY 1", [p.tenant_id]).fetchall()
        worst = c.execute("SELECT v.id, v.vin, (h.signals->>'soh')::numeric AS soh FROM vehicle_health h JOIN vehicles v ON v.id = h.vehicle_id "
                          "WHERE h.tenant_id = %s AND h.signals ? 'soh' ORDER BY (h.signals->>'soh')::numeric LIMIT 10", [p.tenant_id]).fetchall()
    return {"soh_distribution": jsonable(dist), "lowest_soh": jsonable(worst)}


@router.get("/maintenance")
@cached("maintenance", 30)
def maintenance(limit: int = Query(20, ge=1, le=100), p: Principal = Depends(require("analytics:read")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        top = c.execute("SELECT v.id, v.vin, m.make, m.model, p.failure_prob, p.top_factors, p.model_version FROM latest_maintenance_predictions p "
                        "JOIN vehicles v ON v.id = p.vehicle_id JOIN vehicle_models m ON m.id = v.model_id WHERE p.tenant_id = %s "
                        "ORDER BY p.failure_prob DESC LIMIT %s", [p.tenant_id, limit]).fetchall()
        hist = c.execute("SELECT least(width_bucket(failure_prob::float8, 0, 1, 10), 10) AS bucket, count(*) AS n FROM latest_maintenance_predictions "
                         "WHERE tenant_id = %s GROUP BY 1 ORDER BY 1", [p.tenant_id]).fetchall()
    return {"top_risk": jsonable(top), "risk_histogram": jsonable(hist)}


@router.get("/incidents")
@cached("incidents", 30)
def incidents(days: int = Query(14, ge=1, le=90), p: Principal = Depends(require("analytics:read")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rows = c.execute("SELECT date_trunc('day', created_at)::date AS day, alert_type_code AS type, count(*) AS n FROM alerts "
                         "WHERE tenant_id = %s AND created_at >= now() - make_interval(days => %s) GROUP BY 1, 2 ORDER BY 1, 2",
                         [p.tenant_id, days]).fetchall()
    return {"items": jsonable(rows)}


@router.get("/fuel")
@cached("fuel", 30)
def fuel(days: int = Query(14, ge=1, le=90), p: Principal = Depends(require("analytics:read")), ctx: AppContext = Depends(dep_ctx)):
    s = get_settings()
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rows = c.execute("SELECT day, round(fuel_litres::numeric, 1) AS fuel_litres, round(energy_kwh::numeric, 1) AS energy_kwh, "
                         "round((idle_seconds / 3600.0 * %s)::numeric, 1) AS idle_fuel_litres FROM mv_fleet_daily_kpis "
                         "WHERE tenant_id = %s AND day >= current_date - %s ORDER BY day", [s.idle_fuel_lph, p.tenant_id, days]).fetchall()
    return {"items": jsonable(rows), "fuel_price_per_litre": s.fuel_price_per_litre}


@router.get("/top-dtc")
@cached("topdtc", 30)
def top_dtc(p: Principal = Depends(require("analytics:read")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rows = c.execute("SELECT details->>'code' AS code, count(*) AS n FROM alerts WHERE tenant_id = %s AND alert_type_code = 'repeated_dtc' "
                         "AND created_at >= now() - interval '7 days' GROUP BY 1", [p.tenant_id]).fetchall()
    return {"items": [{"code": k, "count": v} for k, v in top_k_exact({r["code"]: r["n"] for r in rows if r["code"]}, 10)]}
