#!/usr/bin/env python
"""Seed the platform with SYNTHETIC data: tenants, users, fleets, 100K vehicles, drivers, history, knowledge base.

  python scripts/seed.py --vehicles 100000
Idempotent: refuses to reseed a non-empty database unless --force (which truncates)."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import orjson  # noqa: E402
import psycopg  # noqa: E402

from cvi.api.security import hash_password  # noqa: E402
from cvi.common.config import get_settings  # noqa: E402
from cvi.ml.features import FEATURES  # noqa: E402
from cvi.ml.synthetic import generate  # noqa: E402
from cvi.storage.pg import migrate  # noqa: E402
from cvi.storage.vector import embed, vec_literal  # noqa: E402
from cvi.synthetic.identity import FLEET_NAMES, MODELS, TENANTS, vehicle_identity  # noqa: E402

log = logging.getLogger("seed")
SHORT = {"acme-logistics": "acme", "borealis-ev": "borealis", "metro-transit": "metro"}
ROLES = {"admin": "Full access incl. audit and erasure", "fleet_manager": "Operate the fleet, acknowledge alerts, use the agent",
         "analyst": "Read-only analytics and agent (no precise location)", "viewer": "Dashboards only (location masked)"}

KNOWLEDGE = [
    ("Engine overheating response", "maintenance", "If coolant temperature exceeds 110 C: reduce load, pull over safely, switch off and let the engine cool for 30 minutes before checking coolant level, hoses, radiator fan and thermostat. Do not open the radiator cap while hot. Repeated overheating usually indicates a failed thermostat, water pump or a coolant leak and the vehicle should not return to service until repaired."),
    ("P0301 cylinder 1 misfire", "diagnostics", "P0301 indicates a misfire in cylinder 1. Common causes are a worn spark plug, faulty ignition coil, clogged injector or low compression. Repeated P0301 occurrences within minutes require an inspection; continuing to drive can damage the catalytic converter."),
    ("P0217 engine overtemperature", "diagnostics", "P0217 is logged when the engine coolant temperature exceeds the safe limit. Combine with oil pressure and vibration signals: a hot engine with falling oil pressure signals imminent engine failure. Stop the vehicle and arrange a tow."),
    ("Low oil pressure procedure", "maintenance", "Oil pressure below 100 kPa while the engine runs above 1200 rpm is critical. Shut the engine down immediately. Check oil level, pump and filter. Running with low oil pressure destroys bearings within minutes."),
    ("EV battery state of health below 80 percent", "battery", "A traction battery below 80 percent state of health has reduced range and higher internal resistance. Schedule a capacity test, avoid frequent DC fast charging and keep the pack between 20 and 80 percent state of charge. Replace or reassign the vehicle to short routes if SoH keeps falling."),
    ("Battery over-temperature", "battery", "Pack temperature above 55 C: stop fast charging, park in shade, inspect the coolant loop and pumps. Persistent thermal alerts increase degradation and fire risk."),
    ("Reducing idling to save fuel", "operations", "Idling burns roughly 2 to 3 litres of fuel per hour for a commercial vehicle and provides no revenue. Introduce an idle-shutdown policy (engine off after 5 minutes parked), coach drivers, and review depot queue times. Typical fleets cut idle time by 20 to 40 percent."),
    ("Defensive driving coaching", "safety", "Drivers with repeated harsh braking, harsh acceleration and speeding events in a 30 minute window are high risk. Review trip data with the driver, set speed alerts, and assign defensive-driving coaching within the week."),
    ("Suspicious movement with engine off", "security", "Movement at speed with the ignition off suggests towing or theft. Contact the driver; if unreachable escalate to security, share last known location with authorities and enable immobilisation if available."),
    ("Geofence breach handling", "security", "A vehicle outside every operating geofence for several consecutive reports needs verification. Confirm the trip is authorised, then update geofences if the route is legitimate."),
    ("Preventive maintenance intervals", "maintenance", "Service light commercial vehicles every 15000 km or 12 months, whichever comes first. Vehicles beyond 25000 km since the last service have markedly higher breakdown risk; prioritise them when workshop capacity is limited."),
    ("Diagnostic trouble code P0420", "diagnostics", "P0420 catalyst efficiency below threshold: check oxygen sensors and exhaust leaks before replacing the catalytic converter. Usually not urgent but increases emissions."),
    ("Diagnostic trouble code P0171", "diagnostics", "P0171 system too lean: inspect for vacuum leaks, a dirty mass airflow sensor or a weak fuel pump. Can accompany misfire codes."),
    ("Vibration anomalies", "maintenance", "Vibration above 2.5 g combined with rising coolant temperature points to mounting, bearing or misfire problems. Inspect engine mounts and wheel bearings."),
]


def build_signal_pool(n: int):
    X, _, _ = generate(n, seed=21)
    ev = X[:, FEATURES.index("is_ev")] == 1
    return X[ev], X[~ev]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicles", type=int, default=100_000)
    ap.add_argument("--history-days", type=int, default=14)
    ap.add_argument("--history-alerts", type=int, default=1_000_000)
    ap.add_argument("--admin-url", default=get_settings().admin_database_url)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-scoring", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    s = get_settings()
    t0 = time.time()
    applied = migrate(a.admin_url, s.app_db_password, s.worker_db_password)
    log.info("migrations applied: %s", applied or "none (up to date)")
    pw_hash = hash_password(s.demo_password)

    with psycopg.connect(a.admin_url, autocommit=False) as c:
        if c.execute("SELECT count(*) FROM tenants").fetchone()[0]:
            if not a.force:
                log.info("database already seeded; use --force to reseed")
                return
            c.execute("TRUNCATE tenants, plans, roles, vehicle_models, alert_types, knowledge_docs, audit_log, agent_audit RESTART IDENTITY CASCADE")
        cur = c.cursor()
        cur.executemany("INSERT INTO plans VALUES (%s,%s,%s,%s)", [("standard", "Standard", 50_000, 99_00), ("enterprise", "Enterprise", 500_000, 999_00)])
        cur.executemany("INSERT INTO roles VALUES (%s,%s)", list(ROLES.items()))
        for t in TENANTS:
            tid = c.execute("INSERT INTO tenants (slug, name) VALUES (%s,%s) RETURNING id", [t.slug, t.name]).fetchone()[0]
            c.execute("INSERT INTO subscriptions (tenant_id, plan_code) VALUES (%s,%s)", [tid, t.plan])
            for role in ROLES:
                uid = c.execute("INSERT INTO users (tenant_id, email, display_name, password_hash) VALUES (%s,%s,%s,%s) RETURNING id",
                                [tid, f"{role}@{SHORT[t.slug]}.demo", f"{role.replace('_', ' ').title()} ({t.name})", pw_hash]).fetchone()[0]
                c.execute("INSERT INTO user_roles VALUES (%s,%s)", [uid, role])
            for fname in FLEET_NAMES:
                c.execute("INSERT INTO fleets (tenant_id, name) VALUES (%s,%s)", [tid, fname])
            c.execute("INSERT INTO geofences (tenant_id, name, kind, center_lat, center_lon, radius_m) VALUES (%s,%s,'operating',%s,%s,%s)",
                      [tid, f"{t.name} service area", t.home_lat, t.home_lon, 70_000])
            c.execute("INSERT INTO geofences (tenant_id, name, kind, center_lat, center_lon, radius_m) VALUES (%s,%s,'restricted',%s,%s,%s)",
                      [tid, "Restricted airfield", t.home_lat + 0.12, t.home_lon + 0.12, 1500])
        cur.executemany("INSERT INTO vehicle_models (make, model, model_year, fuel_type, vehicle_class, capacity) VALUES (%s,%s,%s,%s,%s,%s)", MODELS)
        c.commit()

        tenant_ids = {r[0]: r[1] for r in c.execute("SELECT slug, id FROM tenants")}
        fleet_ids = {(r[0], r[1]): r[2] for r in c.execute("SELECT t.slug, f.name, f.id FROM fleets f JOIN tenants t ON t.id = f.tenant_id")}

        n = a.vehicles
        log.info("generating %s vehicle identities...", f"{n:,}")
        ident = [vehicle_identity(i) for i in range(n)]
        with c.cursor() as cur, cur.copy("COPY vehicles (tenant_id, fleet_id, model_id, vin) FROM STDIN") as cp:
            for d in ident:
                cp.write_row((tenant_ids[d["tenant"]], fleet_ids[(d["tenant"], FLEET_NAMES[d["fleet_idx"]])], d["model_idx"] + 1, d["vin"]))
        c.commit()
        first_id = c.execute("SELECT min(id) FROM vehicles").fetchone()[0]
        assert c.execute("SELECT count(*) FROM vehicles").fetchone()[0] == n
        vid = lambda i: first_id + i  # noqa: E731
        log.info("vehicles loaded: %s", f"{n:,}")

        # signals/features per vehicle (synthetic; same generator as ML training so scoring is meaningful)
        ev_pool, ice_pool = build_signal_pool(max(n, 20_000))
        rng = np.random.default_rng(5)
        today = dt.date.today()
        ci = {f: FEATURES.index(f) for f in FEATURES}
        health_rows, maint_rows = [], []
        ev_i = ice_i = 0
        for i, d in enumerate(ident):
            if d["fuel_type"] == "ev":
                row, ev_i = ev_pool[ev_i % len(ev_pool)], ev_i + 1
            else:
                row, ice_i = ice_pool[ice_i % len(ice_pool)], ice_i + 1
            sig = {"coolant": round(float(row[ci["coolant_ewma_c"]]), 2), "oil": round(float(row[ci["oil_pressure_ewma_kpa"]]), 1),
                   "vib": round(float(row[ci["vibration_ewma_g"]]), 3), "km_since_service": round(float(row[ci["km_since_service"]]), 0),
                   "days_since_service": round(float(row[ci["days_since_service"]]), 0)}
            if d["fuel_type"] == "ev":
                sig["soh"] = round(float(row[ci["battery_soh_pct"]]), 2)
            elif d["fuel_type"] == "hybrid":
                sig["soh"] = round(100 - (100 - float(row[ci["battery_soh_pct"]])) * 0.5, 2)
            health_rows.append((vid(i), tenant_ids[d["tenant"]], int(row[ci["health_score"]]), "{}", orjson.dumps(sig).decode(), None))
            odo = float(rng.uniform(8_000, 160_000))
            maint_rows.append((tenant_ids[d["tenant"]], vid(i), today - dt.timedelta(days=int(sig["days_since_service"])), "scheduled service",
                               int(rng.integers(18_000, 90_000)), round(max(0.0, odo - sig["km_since_service"]), 1), None))
        with c.cursor() as cur:
            with cur.copy("COPY vehicle_health (vehicle_id, tenant_id, health_score, factors, signals, last_event_ts) FROM STDIN") as cp:
                for r_ in health_rows:
                    cp.write_row(r_)
            with cur.copy("COPY maintenance_records (tenant_id, vehicle_id, performed_at, kind, cost_cents, odometer_km, notes) FROM STDIN") as mp:
                for r_ in maint_rows:
                    mp.write_row(r_)
        c.commit()
        log.info("health + maintenance records loaded")

        # drivers: one per ~3 vehicles (pseudonymised, synthetic)
        nd = n // 3
        with c.cursor() as cur:
            with cur.copy("COPY drivers (tenant_id, display_name, licence_hash) FROM STDIN") as dp:
                for k in range(nd):
                    d = ident[k * 3]
                    dp.write_row((tenant_ids[d["tenant"]], f"Driver {k + 1:05d}", "synthetic-" + format(k * 2654435761 % 2**32, "08x")))
        c.commit()
        d_first = c.execute("SELECT min(id) FROM drivers").fetchone()[0]
        with c.cursor() as cur, cur.copy("COPY vehicle_assignments (tenant_id, vehicle_id, driver_id, from_ts) FROM STDIN") as ap_:
            start = dt.datetime.now(dt.UTC) - dt.timedelta(days=90)
            for k in range(nd):
                d = ident[k * 3]
                for j in range(3):
                    idx = k * 3 + j
                    if idx < n and ident[idx]["tenant"] == d["tenant"]:
                        ap_.write_row((tenant_ids[d["tenant"]], vid(idx), d_first + k, start))
        c.commit()
        log.info("drivers loaded: %s", f"{nd:,}")

        # daily stats history (drives analytics trends + materialised view)
        log.info("loading %d days of daily stats for %s vehicles...", a.history_days, f"{n:,}")
        with c.cursor() as cur, cur.copy("COPY daily_vehicle_stats (vehicle_id, day, tenant_id, events, distance_km, drive_seconds, idle_seconds, "
                                          "fuel_litres, energy_kwh, harsh_brake, harsh_accel, speeding) FROM STDIN") as sp:
            for day_off in range(a.history_days, 0, -1):
                day = today - dt.timedelta(days=day_off)
                weekend = day.weekday() >= 5
                active = rng.random(n) < (0.55 if weekend else 0.82)
                dist = np.where(active, rng.gamma(4.0, 38.0, n), 0.0)
                drive = (dist / rng.uniform(35, 55, n) * 3600).astype(int)
                idle = np.where(active, rng.gamma(2.0, 1100.0, n), 0).astype(int)
                for i in range(n):
                    if not active[i]:
                        continue
                    d = ident[i]
                    ev = d["fuel_type"] == "ev"
                    sp.write_row((vid(i), day, tenant_ids[d["tenant"]], int(drive[i] / 30), round(float(dist[i]), 1), int(drive[i]), int(idle[i]),
                                  0.0 if ev else round(float(dist[i] * 0.11 + idle[i] / 3600 * 2.5), 2), round(float(dist[i] * 0.2), 1) if ev else 0.0,
                                  int(rng.poisson(0.8)), int(rng.poisson(0.6)), int(rng.poisson(1.5))))
        c.commit()

    # reference data + historical alerts + knowledge base + predictions
    from cvi.storage.pg import get_pool
    from cvi.stream.pg_sink import seed_alert_types

    wurl = s.worker_database_url or a.admin_url
    pool = get_pool(a.admin_url)
    seed_alert_types(pool)
    with psycopg.connect(a.admin_url) as c:
        for title, cat, content in KNOWLEDGE:
            c.execute("INSERT INTO knowledge_docs (tenant_id, title, category, content, embedding) VALUES (NULL,%s,%s,%s,%s::vector)",
                      [title, cat, content, vec_literal(embed(title + " " + content))])
        if a.history_alerts:
            log.info("generating %s historical alerts in SQL...", f"{a.history_alerts:,}")
            c.execute(
                """
                INSERT INTO alerts (tenant_id, vehicle_id, alert_type_code, severity, status, confidence, cause, recommended_action, event_ts, created_at, dedup_key, details)
                SELECT v.tenant_id, v.id, t.code,
                       (ARRAY['info','warning','warning','critical'])[1 + (g %% 4)],
                       (ARRAY['resolved','resolved','open','acknowledged'])[1 + ((g / 4) %% 4)],
                       0.6 + (g %% 40) / 100.0, 'Historical synthetic alert ' || t.code, 'See maintenance knowledge base',
                       now() - (g %% 1209600) * interval '1 second', now() - (g %% 1209600) * interval '1 second', 'hist:' || g,
                       jsonb_build_object('code', (ARRAY['P0301','P0171','P0420','P0300'])[1 + (g %% 4)], 'synthetic', true)
                FROM generate_series(1::bigint, %(n)s::bigint) g
                JOIN LATERAL (SELECT ((g * 7919) %% %(nv)s) + %(first)s AS vid) pick ON true
                JOIN vehicles v ON v.id = pick.vid
                JOIN (SELECT array_agg(code ORDER BY code) AS codes FROM alert_types) ac ON true
                JOIN alert_types t ON t.code = ac.codes[1 + (g %% cardinality(ac.codes))]
                """, {"n": a.history_alerts, "nv": n, "first": first_id})
        c.commit()
        c.execute("REFRESH MATERIALIZED VIEW mv_fleet_daily_kpis")
        c.execute("ANALYZE")
        c.commit()

    if not a.skip_scoring:
        from cvi.lifecycle.scoring import score_all
        from cvi.ml.model import load_latest
        from cvi.ml.train import train

        if load_latest() is None:
            log.info("no model artifact found -> training")
            train()
            load_latest.cache_clear()
        log.info("scoring maintenance risk for all vehicles...")
        score_all(get_pool(wurl), list(tenant_ids.values()))
    with psycopg.connect(a.admin_url, autocommit=True) as c:
        c.execute("ANALYZE")
    from cvi.storage.pg import close_pools

    close_pools()
    log.info("seed complete in %.0fs (%s vehicles)", time.time() - t0, f"{n:,}")


if __name__ == "__main__":
    main()
