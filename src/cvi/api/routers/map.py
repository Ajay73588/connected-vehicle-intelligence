from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from cvi.api.context import AppContext, dep_ctx
from cvi.api.security import Principal, mask_location, require
from cvi.common.config import get_settings
from cvi.storage.pg import tenant_tx

router = APIRouter(prefix="/api/v1/map", tags=["map"])


@router.get("/vehicles")
def map_vehicles(lat_min: float = Query(..., ge=-90, le=90), lat_max: float = Query(..., ge=-90, le=90),
                 lon_min: float = Query(..., ge=-180, le=180), lon_max: float = Query(..., ge=-180, le=180),
                 limit: int = Query(1500, ge=1, le=5000), p: Principal = Depends(require("vehicles:read")),
                 ctx: AppContext = Depends(dep_ctx)):
    """Viewport query served entirely from Redis: geohash cells -> member VINs -> latest-state hashes (no SQL on the hot path)."""
    cells = ctx.state.cells_for_bbox(lat_min, lat_max, lon_min, lon_max)
    vins = ctx.state.vins_in_cells(p.tenant_slug, cells, limit)
    out = []
    for st in ctx.state.get_states(vins):
        lat, lon = float(st["lat"]), float(st["lon"])
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            continue
        mlat, mlon = mask_location(p, lat, lon)
        health = int(float(st.get("health", 100) or 100))
        speed = float(st.get("speed_kmh", 0) or 0)
        status = "idle" if st.get("idling") in (True, "True", "1") else "moving" if speed > 3 else "parked"
        out.append({"vin": st["vin"], "lat": mlat, "lon": mlon, "speed_kmh": speed, "status": status, "health": health,
                    "risk": int(float(st.get("risk", 0) or 0)),
                    "severity": "critical" if health < 50 else "warning" if health < 75 else "ok", "ts": int(float(st["ts"]))})
    return {"count": len(out), "vehicles": out}


@router.get("/geofences")
def geofences(p: Principal = Depends(require("vehicles:read")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rows = c.execute("SELECT id, name, kind, center_lat, center_lon, radius_m FROM geofences WHERE tenant_id = %s", [p.tenant_id]).fetchall()
    return {"items": rows, "center": {"lat": rows[0]["center_lat"], "lon": rows[0]["center_lon"]} if rows else None, "settings_note": get_settings().env}


@router.get("/lookup/{vin}")
def lookup(vin: str, p: Principal = Depends(require("vehicles:read")), ctx: AppContext = Depends(dep_ctx)):
    st = ctx.state.get_state(vin.upper())
    if not st or st.get("tenant") != p.tenant_slug:
        return {"found": False}
    lat, lon = mask_location(p, float(st["lat"]), float(st["lon"]))
    return {"found": True, "lat": lat, "lon": lon}
