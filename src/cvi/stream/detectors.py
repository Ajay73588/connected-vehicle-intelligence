"""Per-vehicle streaming detectors. Pure logic (no I/O): feed events in, get alert candidates + aggregates out.

All windows are measured in EVENT time, so out-of-order events inside the allowed lateness are placed correctly."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from cvi.algorithms.geo import in_circle
from cvi.algorithms.obd import DTC_CATALOG
from cvi.algorithms.windowing import OnlineStats, SlidingWindow

OVERHEAT_C = 110.0
LOW_OIL_KPA = 100.0
HIGH_VIBRATION_G = 2.5
SEVERITY_PENALTY = {"critical": 25, "high": 15, "medium": 8}
_RANK = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class AlertCandidate:
    type: str
    severity: str
    confidence: float
    cause: str
    recommended_action: str
    ts: int
    details: dict = field(default_factory=dict)


@dataclass
class Geofence:
    name: str
    kind: str  # operating | restricted
    lat: float
    lon: float
    radius_m: float


class VehicleState:
    def __init__(self, vin: str, tenant: str):
        self.vin, self.tenant = vin, tenant
        self.last_ts = 0
        self.last_seq = -1
        self.last_odo: float | None = None
        self.last_fuel: float | None = None
        self.last_soc: float | None = None
        self.lat = self.lon = 0.0
        self.processed: deque[str] = deque(maxlen=256)
        self._results: dict[str, tuple] = {}  # event_id -> outcome, so a retried batch re-emits the SAME alerts/aggregates
        # event-time windows
        self.hot = SlidingWindow(120)
        self.low_oil = SlidingWindow(60)
        self.hb = SlidingWindow(1800)
        self.ha = SlidingWindow(1800)
        self.sp = SlidingWindow(1800)
        self.vib = SlidingWindow(120)
        self.dtc: dict[str, SlidingWindow] = {}
        self.soh = SlidingWindow(3600)
        self.engine_off_moving = SlidingWindow(60)
        self.outside = SlidingWindow(120)
        self.restricted = SlidingWindow(60)
        self.coolant_stats = OnlineStats()
        self.voltage_stats = OnlineStats()
        self.z_hits = SlidingWindow(120)
        self.idle_run_s = 0.0
        self.idle_today_s = 0.0
        self.soh_last: float | None = None
        self.ewma: dict[str, float] = {}  # slow EWMAs of raw signals, used as ML features
        self.gaps = 0
        self.cell: str | None = None  # geohash cell last written to Redis
        self.cell_ts = 0.0
        self.dirty = True
        self.last_flush = 0.0

    # --- idempotence inside the process (protects detector windows when a failed batch is retried) ---
    def already_processed(self, event_id: str) -> bool:
        return event_id in self._results

    def result(self, event_id: str) -> tuple:
        return self._results[event_id]

    def mark_processed(self, event_id: str, outcome: tuple) -> None:
        if len(self.processed) == self.processed.maxlen:
            self._results.pop(self.processed[0], None)
        self.processed.append(event_id)
        self._results[event_id] = outcome

    # --- derived scores --------------------------------------------------------------------------------
    def health(self) -> tuple[int, dict]:
        factors: dict[str, float] = {}
        if self.hot.total >= 3:
            factors["overheating"] = 30
        dtc_pen: float = 0
        for code, w in self.dtc.items():
            if w.total >= 1:
                sev = DTC_CATALOG.get(code, ("", "medium"))[1]
                dtc_pen += SEVERITY_PENALTY[sev] * (1.5 if w.total >= 3 else 1)
        if dtc_pen:
            factors["diagnostic_codes"] = min(50, dtc_pen)
        if self.low_oil.total >= 2:
            factors["low_oil_pressure"] = 20
        if self.soh_last is not None and self.soh_last < 85:
            factors["battery_degradation"] = min(40, round((85 - self.soh_last) * 1.2, 1))
        if self.vib.total >= 3:
            factors["vibration"] = 10
        if self.z_hits.total >= 3:
            factors["sensor_anomaly"] = 10
        score = max(0, round(100 - sum(factors.values())))
        return score, factors

    def signals(self) -> dict:
        return {k: round(v, 3) for k, v in self.ewma.items()} | ({"soh": self.soh_last} if self.soh_last is not None else {})

    def _ew(self, key: str, x: float, alpha: float = 0.02) -> None:
        self.ewma[key] = x if key not in self.ewma else (1 - alpha) * self.ewma[key] + alpha * x

    def safety(self) -> dict:
        hb, ha, sp = int(self.hb.total), int(self.ha.total), int(self.sp.total)
        risk = min(100, 12 * hb + 10 * ha + min(40, sp) + min(15, int(self.idle_today_s / 120)))
        return {"risk_score": risk, "harsh_brake_30m": hb, "harsh_accel_30m": ha, "speeding_30m": sp,
                "idle_seconds_today": int(self.idle_today_s)}


class DetectorEngine:
    """Applies all detectors to one event for the owning VehicleState."""

    def __init__(self, geofences: dict[str, list[Geofence]] | None = None, idle_threshold_s: int = 600,
                 idle_fuel_lph: float = 2.5, fuel_price: float = 1.45):
        self.geofences = geofences or {}
        self.idle_threshold_s, self.idle_fuel_lph, self.fuel_price = idle_threshold_s, idle_fuel_lph, fuel_price

    def update(self, st: VehicleState, e: dict, in_order: bool) -> tuple[list[AlertCandidate], dict[str, float]]:
        ts_s = e["ts"] / 1000.0
        alerts: list[AlertCandidate] = []
        incs: dict[str, float] = {"n": 1.0}

        # ---- driver safety counters (valid for in-order and late-but-inside-lateness events)
        st.hb.add(ts_s, 1.0 if e.get("harsh_brake") else 0.0)
        st.ha.add(ts_s, 1.0 if e.get("harsh_accel") else 0.0)
        st.sp.add(ts_s, 1.0 if e.get("speeding") else 0.0)
        if e.get("harsh_brake"):
            incs["hb"] = 1.0
        if e.get("harsh_accel"):
            incs["ha"] = 1.0
        if e.get("speeding"):
            incs["sp"] = 1.0

        # ---- deltas need a trustworthy predecessor => in-order events only
        if in_order:
            dt = min(120.0, max(0.0, ts_s - st.last_ts / 1000.0)) if st.last_ts else 0.0
            if st.last_odo is not None and e["odometer_km"] >= st.last_odo:
                incs["dist_km"] = min(e["odometer_km"] - st.last_odo, 100.0)
            if e.get("idling"):
                st.idle_run_s += dt
                st.idle_today_s += dt
                incs["idle_s"] = dt
                incs["idle_n"] = 1.0
            else:
                st.idle_run_s = 0.0
                if e["speed_kmh"] > 3:
                    incs["drive_s"] = dt
            fl = e.get("fuel_level_pct")
            if fl is not None and st.last_fuel is not None and fl < st.last_fuel:
                cap = 150.0 if e["fuel_type"] == "hybrid" else 98.0 if e["fuel_type"] == "petrol" else 95.0
                incs["fuel_l"] = (st.last_fuel - fl) / 100.0 * cap
            soc = e.get("battery_soc_pct")
            if soc is not None and st.last_soc is not None and soc < st.last_soc:
                incs["kwh"] = (st.last_soc - soc) / 100.0 * 75.0
            st.last_odo = e["odometer_km"]
            st.last_fuel = fl if fl is not None else st.last_fuel
            st.last_soc = soc if soc is not None else st.last_soc
            st.lat, st.lon = e["lat"], e["lon"]

        # ---- vehicle health
        ct = e.get("coolant_temp_c")
        if ct is not None:
            st.hot.add(ts_s, 1.0 if ct > OVERHEAT_C else 0.0)
            st._ew("coolant", ct)
            z = st.coolant_stats.update(ct)
            st.z_hits.add(ts_s, 1.0 if abs(z) > 4.0 else 0.0)
            if st.hot.total >= 3:
                sev = "critical"
                alerts.append(AlertCandidate(
                    "engine_overheating", sev, min(0.99, 0.7 + 0.05 * st.hot.total),
                    f"Coolant temperature exceeded {OVERHEAT_C:.0f}°C in {int(st.hot.total)} samples within 2 minutes (latest {ct:.0f}°C).",
                    "Stop the vehicle safely, let the engine cool and inspect coolant level, thermostat and radiator before driving again.",
                    e["ts"], {"coolant_temp_c": ct, "samples_over_threshold": int(st.hot.total)}))
        vol = e.get("voltage_v")
        if vol is not None:
            st.voltage_stats.update(vol)

        op, rpm, spd = e.get("oil_pressure_kpa"), e.get("rpm"), e["speed_kmh"]
        if op is not None and rpm is not None and rpm > 1200:
            st._ew("oil", op)
            st.low_oil.add(ts_s, 1.0 if op < LOW_OIL_KPA else 0.0)
            if st.low_oil.total >= 2:
                alerts.append(AlertCandidate(
                    "low_oil_pressure", "critical", 0.9,
                    f"Oil pressure {op:.0f} kPa is below {LOW_OIL_KPA:.0f} kPa while the engine runs at {rpm:.0f} rpm.",
                    "Shut the engine down and arrange a tow; running with low oil pressure risks catastrophic engine damage.",
                    e["ts"], {"oil_pressure_kpa": op, "rpm": rpm}))
        vib = e.get("vibration_g")
        if vib is not None:
            st.vib.add(ts_s, 1.0 if vib > HIGH_VIBRATION_G else 0.0)
            st._ew("vib", vib)
        if ct is not None and rpm is not None and rpm > 3500 and spd < 5 and ct > 100:
            alerts.append(AlertCandidate(
                "sensor_anomaly", "warning", 0.75,
                f"Unusual sensor combination: {rpm:.0f} rpm at {spd:.0f} km/h with {ct:.0f}°C coolant (engine racing while stationary and hot).",
                "Inspect for a stuck throttle, clutch/transmission slip or a cooling fault.", e["ts"],
                {"rpm": rpm, "speed_kmh": spd, "coolant_temp_c": ct}))
        elif st.z_hits.total >= 3:
            alerts.append(AlertCandidate(
                "sensor_anomaly", "warning", 0.7,
                f"Coolant temperature deviates >4σ from this vehicle's own baseline in {int(st.z_hits.total)} recent samples.",
                "Schedule a cooling-system and sensor inspection.", e["ts"], {"coolant_temp_c": ct}))

        for code in e.get("dtc") or []:
            w = st.dtc.setdefault(code, SlidingWindow(600))
            w.add(ts_s, 1.0)
            desc, sev = DTC_CATALOG.get(code, ("Unclassified diagnostic trouble code", "medium"))
            if w.total >= 3:
                alerts.append(AlertCandidate(
                    "repeated_dtc", "critical" if sev in ("critical", "high") else "warning", min(0.98, 0.6 + 0.08 * w.total),
                    f"Diagnostic code {code} ({desc}) reported {int(w.total)} times in 10 minutes.",
                    f"Book a diagnostic inspection for {code}; avoid long trips until it is cleared.", e["ts"],
                    {"code": code, "occurrences_10m": int(w.total), "description": desc}))

        # ---- EV battery
        soh, bt = e.get("battery_soh_pct"), e.get("battery_temp_c")
        if soh is not None:
            st.soh_last = soh
            st.soh.add(ts_s, soh)
            if soh < 80:
                alerts.append(AlertCandidate(
                    "battery_degradation", "critical" if soh < 70 else "warning", 0.85,
                    f"Battery state of health is {soh:.1f}% (threshold 80%).",
                    "Schedule a battery capacity test; plan pack replacement or reduce high-power duty cycles.", e["ts"],
                    {"soh_pct": soh}))
        if bt is not None and bt > 55:
            alerts.append(AlertCandidate(
                "battery_thermal", "critical" if bt > 62 else "warning", 0.8,
                f"Battery pack temperature {bt:.0f}°C is above the 55°C limit.",
                "Stop fast-charging, park in shade and inspect the battery cooling loop.", e["ts"], {"battery_temp_c": bt}))

        # ---- driver safety
        sf = st.safety()
        if sf["risk_score"] >= 70:
            alerts.append(AlertCandidate(
                "dangerous_driving", "critical" if sf["risk_score"] >= 85 else "warning", 0.8,
                f"Driver risk score {sf['risk_score']}: {sf['harsh_brake_30m']} harsh brakes, {sf['harsh_accel_30m']} harsh accelerations "
                f"and {sf['speeding_30m']} speeding samples in the last 30 minutes.",
                "Review the trip with the driver and assign defensive-driving coaching.", e["ts"], dict(sf)))

        # ---- idling
        if st.idle_run_s >= self.idle_threshold_s and in_order:
            litres = st.idle_run_s / 3600.0 * self.idle_fuel_lph
            alerts.append(AlertCandidate(
                "excessive_idling", "warning", 0.9,
                f"Engine idled for {st.idle_run_s / 60:.0f} minutes continuously (~{litres:.1f} L fuel, ~${litres * self.fuel_price:.2f}).",
                "Switch the engine off when parked; consider an idle-shutdown policy.", e["ts"],
                {"idle_minutes": round(st.idle_run_s / 60, 1), "fuel_litres": round(litres, 2), "cost_usd": round(litres * self.fuel_price, 2)}))

        # ---- location / security
        gfs = self.geofences.get(e["tenant"], [])
        operating = [g for g in gfs if g.kind == "operating"]
        if operating:
            inside = any(in_circle(e["lat"], e["lon"], g.lat, g.lon, g.radius_m) for g in operating)
            st.outside.add(ts_s, 0.0 if inside else 1.0)
            if st.outside.total >= 3:
                alerts.append(AlertCandidate(
                    "unexpected_location", "warning", 0.8,
                    f"Vehicle has been outside every operating geofence for {int(st.outside.total)} consecutive reports.",
                    "Contact the driver; verify the trip is authorised.", e["ts"], {"lat": round(e["lat"], 4), "lon": round(e["lon"], 4)}))
        for g in (g for g in gfs if g.kind == "restricted"):
            hit = in_circle(e["lat"], e["lon"], g.lat, g.lon, g.radius_m)
            st.restricted.add(ts_s, 1.0 if hit else 0.0)
            if st.restricted.total >= 2:
                alerts.append(AlertCandidate("restricted_zone", "warning", 0.85, f"Vehicle entered restricted zone '{g.name}'.",
                                             "Verify authorisation for this zone.", e["ts"], {"geofence": g.name}))
        st.engine_off_moving.add(ts_s, 1.0 if (not e.get("engine_on", True) and spd > 10) else 0.0)
        if st.engine_off_moving.total >= 2:
            alerts.append(AlertCandidate(
                "suspicious_movement", "critical", 0.8, "Vehicle is moving with the engine off (possible tow or theft).",
                "Contact the driver; if unexplained, treat as theft and notify security.", e["ts"], {"speed_kmh": spd}))

        # one alert per type per event (strongest wins)
        best: dict[str, AlertCandidate] = {}
        for a in alerts:
            if a.type not in best or _RANK[a.severity] > _RANK[best[a.type].severity]:
                best[a.type] = a
        return list(best.values()), incs
