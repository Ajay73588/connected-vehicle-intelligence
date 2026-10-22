"""Deterministic demo scenarios. Each scripted vehicle emits an event-time-compressed history that ends "now"
(e.g. 40 samples 15 s apart = 10 minutes of behaviour delivered in a second), so the full pipeline can be demonstrated
in seconds and reproduced exactly. Randomness is seeded; vehicles are fixed by VIN (see SCENARIO_VEHICLES)."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from cvi.synthetic.identity import vehicle_identity


def _find(pred: Callable[[dict], bool], start: int = 0, count: int = 1) -> list[int]:
    out: list[int] = []
    i = start
    while len(out) < count:
        if pred(vehicle_identity(i)):
            out.append(i)
        i += 1
    return out


_ENGINE = _find(lambda d: d["tenant"] == "acme-logistics" and d["fuel_type"] == "diesel")[0]
_DANGER = _find(lambda d: d["tenant"] == "acme-logistics" and d["fuel_type"] == "petrol")[0]
_EVBAT = _find(lambda d: d["tenant"] == "borealis-ev")[0]
_IDLERS = _find(lambda d: d["tenant"] == "metro-transit", count=6)
_THEFT = _find(lambda d: d["tenant"] == "borealis-ev", start=_EVBAT + 1)[0]

SCENARIO_VEHICLES: dict[str, list[int]] = {
    "engine_failure": [_ENGINE],
    "dangerous_driver": [_DANGER],
    "ev_battery": [_EVBAT],
    "fleet_idling": _IDLERS,
    "suspicious_movement": [_THEFT],
}
RESERVED: set[int] = {i for v in SCENARIO_VEHICLES.values() for i in v}


def _base(i: int, k: int, ts: int, run: int, odo0: float, **kw) -> dict:
    d = vehicle_identity(i)
    e = {"schema_version": 1, "event_id": f"{d['vin']}-s{run}-{k}", "tenant": d["tenant"], "vin": d["vin"], "seq": run * 10_000 + k,
         "ts": ts, "lat": round(d["home_lat"] + 0.002 * k, 6), "lon": round(d["home_lon"] + 0.002 * k, 6), "speed_kmh": 60.0,
         "odometer_km": round(odo0 + k * 0.25, 2), "fuel_type": d["fuel_type"], "harsh_brake": False, "harsh_accel": False,
         "idling": False, "speeding": False, "engine_on": True, "voltage_v": 13.9, "dtc": [], "trip_id": f"{d['vin']}-demo{run}"}
    if d["fuel_type"] != "ev":
        e.update(rpm=2100.0, fuel_level_pct=round(70 - k * 0.1, 1), coolant_temp_c=90.0, oil_temp_c=98.0, oil_pressure_kpa=290.0, vibration_g=0.4)
    if d["fuel_type"] in ("ev", "hybrid"):
        e.update(battery_soc_pct=round(80 - k * 0.2, 1), battery_soh_pct=92.0, battery_temp_c=30.0)
    e.update(kw)
    return e


def engine_failure(now_ms: int, run: int, rnd: random.Random) -> list[dict]:
    i, n, step = _ENGINE, 40, 15_000
    out = []
    for k in range(n):
        ts = now_ms - (n - 1 - k) * step
        cool = 90 + max(0, k - 8) * 1.1 + rnd.uniform(-1, 1)
        kw: dict = {"coolant_temp_c": round(min(cool, 128), 1), "vibration_g": round(0.4 + max(0, k - 14) * 0.15, 2)}
        if k >= 14 and k % 2 == 0:
            kw["dtc"] = ["P0301"]
        if k >= 24 and k % 3 == 0:
            kw["dtc"] = kw.get("dtc", []) + ["P0217"]
        if k >= 28:
            kw["oil_pressure_kpa"] = round(75 - (k - 28) * 2, 0)
        out.append(_base(i, k, ts, run, 84_200.0, **kw))
    return out


def dangerous_driver(now_ms: int, run: int, rnd: random.Random) -> list[dict]:
    i, n, step = _DANGER, 60, 20_000
    out = []
    for k in range(n):
        ts = now_ms - (n - 1 - k) * step
        out.append(_base(i, k, ts, run, 61_500.0, speed_kmh=round(115 + rnd.uniform(0, 25), 1), speeding=True,
                         harsh_brake=(k % 3 == 0), harsh_accel=(k % 4 == 1), rpm=3900.0))
    return out


def ev_battery(now_ms: int, run: int, rnd: random.Random) -> list[dict]:
    i, n, step = _EVBAT, 50, 20_000
    out = []
    for k in range(n):
        ts = now_ms - (n - 1 - k) * step
        out.append(_base(i, k, ts, run, 41_000.0, battery_soh_pct=round(79.5 - k * 0.2, 2), battery_temp_c=round(38 + k * 0.5, 1),
                         battery_soc_pct=round(88 - k * 1.1, 1)))
    return out


def fleet_idling(now_ms: int, run: int, rnd: random.Random) -> list[dict]:
    n, step = 50, 20_000
    out = []
    for i in _IDLERS:
        for k in range(n):
            ts = now_ms - (n - 1 - k) * step
            out.append(_base(i, k, ts, run, 120_000.0, speed_kmh=0.0, idling=True, rpm=780.0, fuel_level_pct=round(60 - k * 0.12, 1)))
    out.sort(key=lambda e: e["ts"])
    return out


def suspicious_movement(now_ms: int, run: int, rnd: random.Random) -> list[dict]:
    i, n, step = _THEFT, 12, 10_000
    return [_base(i, k, now_ms - (n - 1 - k) * step, run, 22_000.0, engine_on=False, speed_kmh=35.0) for k in range(n)]


SCENARIOS: dict[str, Callable[[int, int, random.Random], list[dict]]] = {
    "engine_failure": engine_failure,
    "dangerous_driver": dangerous_driver,
    "ev_battery": ev_battery,
    "fleet_idling": fleet_idling,
    "suspicious_movement": suspicious_movement,
}

DESCRIPTIONS = {
    "engine_failure": "Diesel van: coolant climbs past 110°C, P0301/P0217 repeat, oil pressure collapses.",
    "dangerous_driver": "Petrol pickup: sustained speeding with repeated harsh braking and acceleration.",
    "ev_battery": "EV van: SoH falls below 80% while the pack temperature rises past 55°C.",
    "fleet_idling": "Six transit buses idle ~16 minutes each: fuel waste and cost at fleet level.",
    "suspicious_movement": "EV moving at 35 km/h with the ignition off (tow/theft indicator).",
}


def run_scenario(name: str, sink, seed: int = 1234, now_ms: int | None = None) -> int:
    """Generate the scenario's events and push them through the sink. Returns the event count."""
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; choose from {sorted(SCENARIOS)}")
    import orjson

    now_ms = now_ms or int(time.time() * 1000)
    run = (now_ms // 1000) % 100_000  # unique per execution -> fresh event ids, strictly increasing seq
    events = SCENARIOS[name](now_ms, run, random.Random(seed))
    sink.send([(e["vin"], orjson.dumps(e)) for e in events])
    return len(events)
