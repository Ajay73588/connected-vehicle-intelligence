"""Synthetic maintenance dataset generator.

IMPORTANT: there is no real failure data in this project. A hidden per-vehicle *wear* variable drives both the observed
features (hotter coolant, lower oil pressure, more DTC alerts, ...) and the probability of a failure within the next 14
days. Model quality measured on this data therefore says how well the pipeline recovers a known synthetic structure —
NOT how accurate it would be on real vehicles. Label noise and feature noise are included so the task is not trivial."""

from __future__ import annotations

import numpy as np

from cvi.ml.features import FEATURES


def generate(n: int, seed: int = 11) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (X, y, wear). X columns follow cvi.ml.features.FEATURES."""
    r = np.random.default_rng(seed)
    wear = np.clip(r.beta(1.2, 4.0, n), 0, 1)                      # hidden: most vehicles healthy, a long tail worn
    is_ev = (r.random(n) < 0.3).astype(float)
    age = r.uniform(0.5, 11, n)
    km_since = np.clip(r.gamma(2.0, 4500, n) * (0.7 + 0.8 * wear), 0, 60_000)
    days_since = np.clip(km_since / r.uniform(40, 160, n), 1, 700)
    ice = 1 - is_ev
    coolant = 88 + ice * (4 + 22 * wear) + r.normal(0, 2.5, n)
    overheat = r.poisson(ice * 6.0 * wear**2)
    dtc = r.poisson(0.2 + 7.0 * wear**1.5 + (km_since / 60_000) * 1.0)
    low_oil = r.poisson(ice * 4.0 * wear**2.2)
    oil = 280 - ice * 130 * wear + r.normal(0, 15, n)
    vib = 0.35 + 1.6 * wear + np.abs(r.normal(0, 0.15, n))
    soh = np.where(is_ev == 1, np.clip(98 - 28 * wear - age * 0.6 + r.normal(0, 2, n), 55, 100), 100.0)
    harsh = np.clip(r.gamma(1.5, 2.0, n) * (0.6 + 1.2 * wear), 0, 60)
    idle = np.clip(r.beta(2, 8, n) + 0.1 * wear, 0, 0.9)
    health = np.clip(100 - 55 * wear - 4 * overheat - 3 * dtc + r.normal(0, 4, n), 0, 100)

    logit = (-5.2 + 6.0 * wear + 0.25 * np.minimum(dtc, 8) + 0.35 * np.minimum(overheat, 6) + 0.9 * (km_since > 25_000)
             + 0.8 * ((soh < 75) & (is_ev == 1)) + 0.04 * age + r.normal(0, 0.6, n))
    p = 1 / (1 + np.exp(-logit))
    y = (r.random(n) < p).astype(int)

    cols = {"coolant_ewma_c": coolant, "overheat_alerts_7d": overheat, "dtc_alerts_7d": dtc, "low_oil_alerts_7d": low_oil,
            "oil_pressure_ewma_kpa": oil, "vibration_ewma_g": vib, "battery_soh_pct": soh, "is_ev": is_ev,
            "harsh_per_100km_7d": harsh, "idle_ratio_7d": idle, "km_since_service": km_since, "days_since_service": days_since,
            "vehicle_age_years": age, "health_score": health}
    X = np.column_stack([cols[f] for f in FEATURES])
    return X, y, wear
