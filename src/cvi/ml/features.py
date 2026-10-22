"""Feature definitions for predictive maintenance. The SAME definitions are used for (synthetic) training and for
production inference, where values come from PostgreSQL (vehicle_health.signals, alerts, daily stats, maintenance)."""

from __future__ import annotations

import numpy as np

FEATURES = [
    "coolant_ewma_c",        # slow EWMA of coolant temperature
    "overheat_alerts_7d",
    "dtc_alerts_7d",
    "low_oil_alerts_7d",
    "oil_pressure_ewma_kpa",
    "vibration_ewma_g",
    "battery_soh_pct",       # 100 for non-EV (no traction battery)
    "is_ev",
    "harsh_per_100km_7d",
    "idle_ratio_7d",
    "km_since_service",
    "days_since_service",
    "vehicle_age_years",
    "health_score",
]
HORIZON_DAYS = 14
FEATURE_LABELS = {
    "coolant_ewma_c": "elevated average coolant temperature",
    "overheat_alerts_7d": "recent engine-overheating alerts",
    "dtc_alerts_7d": "repeated diagnostic trouble codes",
    "low_oil_alerts_7d": "recent low-oil-pressure alerts",
    "oil_pressure_ewma_kpa": "low average oil pressure",
    "vibration_ewma_g": "elevated vibration",
    "battery_soh_pct": "degraded battery state of health",
    "is_ev": "EV powertrain",
    "harsh_per_100km_7d": "harsh driving events per 100 km",
    "idle_ratio_7d": "high idle ratio",
    "km_since_service": "kilometres since last service",
    "days_since_service": "days since last service",
    "vehicle_age_years": "vehicle age",
    "health_score": "low current health score",
}


def to_matrix(rows: list[dict]) -> np.ndarray:
    return np.array([[float(r.get(f, 0.0)) for f in FEATURES] for r in rows], dtype=np.float64)
