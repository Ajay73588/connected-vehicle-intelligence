"""PostgreSQL implementation of the stream processor's AlertSink (uses the BYPASSRLS worker role)."""

from __future__ import annotations

import datetime as dt

import orjson
from psycopg_pool import ConnectionPool

from cvi.storage.pg import plain_tx
from cvi.storage.vector import embed, vec_literal
from cvi.stream.detectors import Geofence

_ALERT_TYPES = {
    "engine_overheating": ("health", "Engine overheating", "critical"),
    "low_oil_pressure": ("health", "Low oil pressure", "critical"),
    "repeated_dtc": ("health", "Repeated diagnostic trouble code", "warning"),
    "sensor_anomaly": ("health", "Abnormal sensor behaviour", "warning"),
    "battery_degradation": ("battery", "Battery degradation", "warning"),
    "battery_thermal": ("battery", "Battery over-temperature", "warning"),
    "dangerous_driving": ("safety", "Dangerous driving pattern", "warning"),
    "excessive_idling": ("fleet", "Excessive idling", "warning"),
    "unexpected_location": ("fleet", "Unexpected location", "warning"),
    "restricted_zone": ("security", "Restricted zone entry", "warning"),
    "suspicious_movement": ("security", "Suspicious movement", "critical"),
}


def seed_alert_types(pool: ConnectionPool) -> None:
    with plain_tx(pool) as c:
        for code, (cat, title, sev) in _ALERT_TYPES.items():
            c.execute("INSERT INTO alert_types (code, category, title, default_severity) VALUES (%s,%s,%s,%s) "
                      "ON CONFLICT (code) DO NOTHING", [code, cat, title, sev])


def _ts(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC)


class PgSink:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def resolve_vehicles(self, vins: list[str]) -> dict[str, tuple[int, int, str]]:
        with self.pool.connection() as c:
            rows = c.execute("SELECT v.vin, v.id, v.tenant_id, t.slug FROM vehicles v JOIN tenants t ON t.id = v.tenant_id "
                             "WHERE v.vin = ANY(%s) AND v.erased_at IS NULL", [vins]).fetchall()
        return {r["vin"]: (r["id"], r["tenant_id"], r["slug"]) for r in rows}

    def load_geofences(self) -> dict[str, list[Geofence]]:
        with self.pool.connection() as c:
            rows = c.execute("SELECT t.slug, g.name, g.kind, g.center_lat, g.center_lon, g.radius_m FROM geofences g "
                             "JOIN tenants t ON t.id = g.tenant_id").fetchall()
        out: dict[str, list[Geofence]] = {}
        for r in rows:
            out.setdefault(r["slug"], []).append(Geofence(r["name"], r["kind"], r["center_lat"], r["center_lon"], r["radius_m"]))
        return out

    def persist_alerts(self, rows: list[dict]) -> list[dict]:
        inserted = []
        with plain_tx(self.pool) as c:
            for r in rows:
                got = c.execute(
                    "INSERT INTO alerts (tenant_id, vehicle_id, alert_type_code, severity, confidence, cause, recommended_action, "
                    "details, event_ts, dedup_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) "
                    "ON CONFLICT (dedup_key) DO NOTHING RETURNING id",
                    [r["tenant_id"], r["vehicle_id"], r["type"], r["severity"], r["confidence"], r["cause"],
                     r["recommended_action"], orjson.dumps(r["details"]).decode(), _ts(r["event_ts"]), r["dedup_key"]]).fetchone()
                if got:
                    summary = f"{r['type']} ({r['severity']}) vehicle {r['vin']}: {r['cause']}"
                    c.execute("INSERT INTO incident_embeddings (alert_id, tenant_id, summary, embedding) VALUES (%s,%s,%s,%s::vector)",
                              [got["id"], r["tenant_id"], summary, vec_literal(embed(summary))])
                    inserted.append({**r, "id": got["id"]})
        return inserted

    def upsert_snapshots(self, rows: list[dict]) -> None:
        """Bulk upsert (psycopg executemany runs in pipeline mode: one network round-trip for the whole batch)."""
        if not rows:
            return
        health = [(r["vehicle_id"], r["tenant_id"], r["health_score"], orjson.dumps(r["factors"]).decode(),
                   orjson.dumps(r["signals"]).decode(), _ts(r["last_event_ts"]) if r["last_event_ts"] else None) for r in rows]
        safety = [(r["vehicle_id"], r["tenant_id"], r["risk_score"], r["harsh_brake_30m"], r["harsh_accel_30m"], r["speeding_30m"],
                   r["idle_seconds_today"]) for r in rows]
        with plain_tx(self.pool) as c, c.cursor() as cur:
            cur.executemany(
                "INSERT INTO vehicle_health (vehicle_id, tenant_id, health_score, factors, signals, last_event_ts) "
                "VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s) ON CONFLICT (vehicle_id) DO UPDATE SET health_score = EXCLUDED.health_score, "
                "factors = EXCLUDED.factors, signals = vehicle_health.signals || EXCLUDED.signals, last_event_ts = EXCLUDED.last_event_ts, "
                "updated_at = now()", health)
            cur.executemany(
                "INSERT INTO vehicle_safety (vehicle_id, tenant_id, risk_score, harsh_brake_30m, harsh_accel_30m, "
                "speeding_30m, idle_seconds_today) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (vehicle_id) DO UPDATE SET "
                "risk_score = EXCLUDED.risk_score, harsh_brake_30m = EXCLUDED.harsh_brake_30m, "
                "harsh_accel_30m = EXCLUDED.harsh_accel_30m, speeding_30m = EXCLUDED.speeding_30m, "
                "idle_seconds_today = EXCLUDED.idle_seconds_today, updated_at = now()", safety)

    def flush_daily(self, rows: list[dict]) -> None:
        if not rows:
            return
        with plain_tx(self.pool) as c, c.cursor() as cur:
            cur.executemany(
                "INSERT INTO daily_vehicle_stats (vehicle_id, day, tenant_id, events, distance_km, drive_seconds, idle_seconds, "
                "fuel_litres, energy_kwh, harsh_brake, harsh_accel, speeding) VALUES "
                "(%(vehicle_id)s,%(day)s,%(tenant_id)s,%(events)s,%(distance_km)s,%(drive_seconds)s,%(idle_seconds)s,"
                "%(fuel_litres)s,%(energy_kwh)s,%(harsh_brake)s,%(harsh_accel)s,%(speeding)s) "
                "ON CONFLICT (vehicle_id, day) DO UPDATE SET events = EXCLUDED.events, distance_km = EXCLUDED.distance_km, "
                "drive_seconds = EXCLUDED.drive_seconds, idle_seconds = EXCLUDED.idle_seconds, fuel_litres = EXCLUDED.fuel_litres, "
                "energy_kwh = EXCLUDED.energy_kwh, harsh_brake = EXCLUDED.harsh_brake, harsh_accel = EXCLUDED.harsh_accel, "
                "speeding = EXCLUDED.speeding", rows)
