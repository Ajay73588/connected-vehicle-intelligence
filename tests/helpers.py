"""Test helpers: an in-memory AlertSink (so processor logic can be unit tested without PostgreSQL) and event builders."""

from __future__ import annotations

import itertools
import time

import orjson

from cvi.algorithms.vin import make_vin
from cvi.messaging.base import TOPIC_VALIDATED, Message
from cvi.stream.detectors import Geofence

VIN = make_vin("1FT", 7)
OTHER_VIN = make_vin("5YJ", 9)


class MemorySink:
    def __init__(self, vehicles: dict[str, tuple[int, int, str]] | None = None):
        self.vehicles = vehicles or {VIN: (1, 1, "acme-logistics"), OTHER_VIN: (2, 2, "borealis-ev")}
        self.alerts: dict[str, dict] = {}
        self.snapshots: dict[int, dict] = {}
        self.daily: dict[tuple, dict] = {}
        self.fail_alerts = 0  # number of upcoming persist_alerts calls that should fail
        self.geofences: dict[str, list[Geofence]] = {}
        self._ids = itertools.count(1)

    def resolve_vehicles(self, vins):
        return {v: self.vehicles[v] for v in vins if v in self.vehicles}

    def persist_alerts(self, rows):
        if self.fail_alerts:
            self.fail_alerts -= 1
            raise ConnectionError("postgres down")
        out = []
        for r in rows:
            if r["dedup_key"] in self.alerts:
                continue
            self.alerts[r["dedup_key"]] = {**r, "id": next(self._ids)}
            out.append(self.alerts[r["dedup_key"]])
        return out

    def upsert_snapshots(self, rows):
        for r in rows:
            self.snapshots[r["vehicle_id"]] = r

    def flush_daily(self, rows):
        for r in rows:
            self.daily[(r["vehicle_id"], r["day"])] = r

    def load_geofences(self):
        return self.geofences


def event(seq: int, ts_ms: int | None = None, vin: str = VIN, tenant: str = "acme-logistics", **kw) -> dict:
    e = {"schema_version": 1, "event_id": f"{vin}-{seq}", "tenant": tenant, "vin": vin, "seq": seq,
         "ts": ts_ms if ts_ms is not None else int(time.time() * 1000) + seq * 1000,
         "lat": 41.88, "lon": -87.63, "speed_kmh": 50.0, "odometer_km": 1000.0 + seq * 0.1, "fuel_type": "diesel",
         "harsh_brake": False, "harsh_accel": False, "idling": False, "speeding": False, "engine_on": True,
         "voltage_v": 13.9, "dtc": [], "rpm": 2000.0, "fuel_level_pct": 60.0, "coolant_temp_c": 90.0, "oil_temp_c": 98.0,
         "oil_pressure_kpa": 280.0, "vibration_g": 0.4, "trip_id": "t1"}
    e.update(kw)
    return e


def msgs(events: list[dict], start_offset: int = 0) -> list[Message]:
    now = str(int(time.time() * 1000))
    return [Message(TOPIC_VALIDATED, e["vin"], orjson.dumps(e), 0, start_offset + i, {"ingest_ts": now}) for i, e in enumerate(events)]


def pump(ctx, rounds: int = 5):
    """Synchronously drive the real pipeline (validator -> stream processor) over whatever is in the in-memory broker."""
    from cvi.common.config import get_settings
    from cvi.ingestion.validator import Validator
    from cvi.messaging.base import TOPIC_VALIDATED
    from cvi.storage.pg import get_pool
    from cvi.stream.pg_sink import PgSink
    from cvi.stream.processor import Processor

    if not hasattr(ctx, "_pump"):
        s = get_settings()
        v = Validator(ctx.broker, "validator-test")
        proc = Processor(ctx.broker, ctx.state, ctx.raw, ctx.objects, PgSink(get_pool(s.worker_database_url)), parquet_flush_s=0)
        ctx._pump = (v, proc, ctx.broker.subscribe("stream-test", [TOPIC_VALIDATED]))
    v, proc, cons = ctx._pump
    total = 0
    for _ in range(rounds):
        raw = v.consumer.poll_batch(10_000, 0)
        if raw:
            v.handle(raw)
            v.consumer.commit(raw)
        batch = cons.poll_batch(10_000, 0)
        if batch:
            proc.process_batch(batch)
            cons.commit(batch)
            total += len(batch)
        if not raw and not batch:
            break
    proc.flush()
    return total
