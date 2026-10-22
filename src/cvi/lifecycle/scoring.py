"""Batch predictive-maintenance scoring: PostgreSQL features -> model -> maintenance_predictions."""

from __future__ import annotations

import logging
from datetime import date

import orjson
from psycopg_pool import ConnectionPool

from cvi.ml.model import MaintenanceModel, load_latest

logger = logging.getLogger("scoring")

FEATURE_SQL = """
SELECT v.id AS vehicle_id, v.tenant_id, EXTRACT(year FROM now())::int - m.model_year AS vehicle_age_years, m.fuel_type = 'ev' AS is_ev,
       coalesce(h.health_score, 100) AS health_score, coalesce(h.signals, '{}'::jsonb) AS signals,
       coalesce(al.overheat, 0) AS overheat_alerts_7d, coalesce(al.dtc, 0) AS dtc_alerts_7d, coalesce(al.oil, 0) AS low_oil_alerts_7d,
       coalesce(ds.harsh * 100.0 / nullif(ds.km, 0), 0) AS harsh_per_100km_7d,
       coalesce(ds.idle_s / nullif(ds.idle_s + ds.drive_s, 0), 0) AS idle_ratio_7d,
       mr.odometer_km AS service_odo, mr.performed_at AS service_date,
       coalesce(ds.odo_hint, 0) AS odo_hint
FROM vehicles v
JOIN vehicle_models m ON m.id = v.model_id
LEFT JOIN vehicle_health h ON h.vehicle_id = v.id
LEFT JOIN LATERAL (SELECT count(*) FILTER (WHERE alert_type_code = 'engine_overheating') AS overheat,
                          count(*) FILTER (WHERE alert_type_code = 'repeated_dtc') AS dtc,
                          count(*) FILTER (WHERE alert_type_code = 'low_oil_pressure') AS oil
                   FROM alerts a WHERE a.vehicle_id = v.id AND a.created_at >= now() - interval '7 days') al ON true
LEFT JOIN LATERAL (SELECT sum(harsh_brake + harsh_accel) AS harsh, sum(distance_km) AS km, sum(idle_seconds) AS idle_s,
                          sum(drive_seconds) AS drive_s, max(distance_km) AS odo_hint
                   FROM daily_vehicle_stats d WHERE d.vehicle_id = v.id AND d.day >= current_date - 7) ds ON true
LEFT JOIN LATERAL (SELECT odometer_km, performed_at FROM maintenance_records r WHERE r.vehicle_id = v.id
                   ORDER BY performed_at DESC LIMIT 1) mr ON true
WHERE v.tenant_id = ANY(%s) AND v.erased_at IS NULL AND v.id > %s
ORDER BY v.id LIMIT %s
"""


def _row_to_features(r: dict) -> dict:
    sig = r["signals"] or {}
    return {
        "coolant_ewma_c": sig.get("coolant", 90.0), "overheat_alerts_7d": r["overheat_alerts_7d"], "dtc_alerts_7d": r["dtc_alerts_7d"],
        "low_oil_alerts_7d": r["low_oil_alerts_7d"], "oil_pressure_ewma_kpa": sig.get("oil", 280.0), "vibration_ewma_g": sig.get("vib", 0.4),
        "battery_soh_pct": sig.get("soh", 100.0), "is_ev": 1.0 if r["is_ev"] else 0.0,
        "harsh_per_100km_7d": float(r["harsh_per_100km_7d"] or 0), "idle_ratio_7d": float(r["idle_ratio_7d"] or 0),
        "km_since_service": float(sig.get("km_since_service", 8000.0)),
        "days_since_service": float((date.today() - r["service_date"]).days) if r["service_date"] else float(sig.get("days_since_service", 120.0)),
        "vehicle_age_years": float(r["vehicle_age_years"]), "health_score": float(r["health_score"]),
    }


def score_all(pool: ConnectionPool, tenant_ids: list[int], model: MaintenanceModel | None = None, chunk: int = 20_000,
              explain_threshold: float = 0.35) -> int:
    model = model or load_latest()
    if model is None:
        raise RuntimeError("no trained model found; run `python -m cvi.ml.train` first")
    total, last = 0, 0
    while True:
        with pool.connection() as c:
            rows = c.execute(FEATURE_SQL, [tenant_ids, last, chunk]).fetchall()
        if not rows:
            return total
        feats = [_row_to_features(r) for r in rows]
        probs = model.predict(feats)
        out = []
        for r, f, p in zip(rows, feats, probs, strict=True):
            factors = model.explain(f) if p >= explain_threshold else []
            out.append((r["tenant_id"], r["vehicle_id"], model.version, round(float(p), 4), 14, orjson.dumps(factors).decode()))
        with pool.connection() as c, c.transaction():
            with c.cursor() as cur:
                cur.executemany("INSERT INTO maintenance_predictions (tenant_id, vehicle_id, model_version, failure_prob, horizon_days, top_factors) "
                                "VALUES (%s,%s,%s,%s,%s,%s::jsonb)", out)
        total += len(rows)
        last = rows[-1]["vehicle_id"]
        logger.info("scored %d vehicles", total)


if __name__ == "__main__":  # python -m cvi.lifecycle.scoring  (re-score every vehicle with the latest model)
    from cvi.common.config import get_settings
    from cvi.common.logging import setup_logging
    from cvi.storage.pg import get_pool

    s = get_settings()
    setup_logging("scoring", s.log_level)
    pool = get_pool(s.worker_database_url or s.database_url)
    with pool.connection() as c:
        tenant_ids = [r["id"] for r in c.execute("SELECT id FROM tenants")]
    print("scored", score_all(pool, tenant_ids), "vehicles")
    from cvi.storage.pg import close_pools

    close_pools()
