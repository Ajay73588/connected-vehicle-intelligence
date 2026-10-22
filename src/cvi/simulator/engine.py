"""Vectorised fleet physics: generates realistic telemetry for 100,000+ vehicles.

Every vehicle has persistent state (position, heading, speed, odometer, fuel/SoC, temperatures, drive mode) that evolves
with each emitted sample, plus per-vehicle fault profiles (a small configurable share of the fleet degrades over time).
State lives in NumPy arrays so a whole batch of samples is advanced in one vectorised step; only the final dict
construction is a Python loop."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from cvi.synthetic.identity import vehicle_identity

MODE_PARKED, MODE_DRIVING, MODE_IDLE = 0, 1, 2
PROFILE_NONE, PROFILE_OVERHEAT, PROFILE_DTC, PROFILE_BATTERY, PROFILE_AGGRESSIVE, PROFILE_IDLER = range(6)
FUEL_IDX = {"diesel": 0, "petrol": 1, "ev": 2, "hybrid": 3}
FUEL_NAMES = ["diesel", "petrol", "ev", "hybrid"]


@dataclass
class ChaosConfig:
    duplicate_rate: float = 0.01
    out_of_order_rate: float = 0.02
    late_rate: float = 0.003
    malformed_rate: float = 0.003
    outage_vehicle_share: float = 0.0  # share of vehicles that buffer data and flush it in a recovery burst


class FleetEngine:
    def __init__(self, vehicles: int, seed: int = 42, shard: int = 0, shards: int = 1,
                 reserved: set[int] | None = None, anomaly_rate: float = 0.015):
        reserved = reserved or set()
        self.rng = np.random.default_rng(seed + shard)
        ids = [i for i in range(vehicles) if i % shards == shard and i not in reserved]
        self.n = len(ids)
        ident = [vehicle_identity(i) for i in ids]
        self.vins = [d["vin"] for d in ident]
        self.tenants = [d["tenant"] for d in ident]
        self.fuel = np.array([FUEL_IDX[d["fuel_type"]] for d in ident], dtype=np.int8)
        self.home_lat = np.array([d["home_lat"] for d in ident])
        self.home_lon = np.array([d["home_lon"] for d in ident])
        n, r = self.n, self.rng
        self.lat, self.lon = self.home_lat.copy(), self.home_lon.copy()
        self.heading = r.uniform(0, 2 * math.pi, n)
        self.mode = r.choice([MODE_PARKED, MODE_DRIVING, MODE_IDLE], n, p=[0.35, 0.55, 0.10]).astype(np.int8)
        self.speed = np.where(self.mode == MODE_DRIVING, r.uniform(25, 80, n), 0.0)
        self.limit = r.choice([80.0, 90.0, 100.0, 110.0], n)
        self.odo = r.uniform(5_000, 150_000, n)
        self.fuel_pct = r.uniform(25, 100, n)
        self.soc = r.uniform(30, 100, n)
        self.soh = r.uniform(86, 99, n)
        self.coolant = np.where(self.mode == MODE_PARKED, r.uniform(20, 40, n), r.uniform(85, 95, n))
        self.bat_temp = r.uniform(20, 38, n)
        self.profile = np.zeros(n, dtype=np.int8)
        n_anom = int(n * anomaly_rate)
        if n_anom:
            pick = r.choice(n, n_anom, replace=False)
            self.profile[pick] = r.choice([PROFILE_OVERHEAT, PROFILE_DTC, PROFILE_BATTERY, PROFILE_AGGRESSIVE, PROFILE_IDLER], n_anom)
        # battery profile only makes sense for EV/hybrid; EVs with that profile start with low SoH
        bat_mask = (self.profile == PROFILE_BATTERY) & (self.fuel >= 2)
        self.soh[bat_mask] = r.uniform(66, 79, bat_mask.sum())
        self.profile[(self.profile == PROFILE_BATTERY) & (self.fuel < 2)] = PROFILE_NONE
        self.seq = np.zeros(n, dtype=np.int64)
        self.last_ms = np.zeros(n, dtype=np.int64)
        self.order = r.permutation(n)
        self.cursor = 0
        self.trip_no = np.zeros(n, dtype=np.int32)
        self.active = np.ones(n, dtype=bool)

    # ------------------------------------------------------------------------------------------------
    def step(self, k: int, now_ms: int) -> list[dict]:
        """Emit k samples (round-robin over the fleet so every vehicle reports at a similar cadence)."""
        if self.n == 0:
            return []
        k = min(k, self.n)
        pos = (self.cursor + np.arange(k)) % self.n
        self.cursor = (self.cursor + k) % self.n
        i = self.order[pos]
        r = self.rng
        prev = self.last_ms[i]
        dt = np.where(prev == 0, 5.0, np.clip((now_ms - prev) / 1000.0, 0.2, 120.0))
        self.last_ms[i] = now_ms

        mode, prof, fuel = self.mode[i], self.profile[i], self.fuel[i]
        # ---- mode transitions
        u = r.random(k)
        p_leave_park = np.where(prof == PROFILE_IDLER, 0.05, 0.02)
        to_drive = (mode == MODE_PARKED) & (u < p_leave_park)
        to_idle = (mode == MODE_DRIVING) & (u < 0.02) | (mode == MODE_PARKED) & (u > 0.995)
        idler_stay = (mode == MODE_IDLE) & (prof == PROFILE_IDLER) & (u > 0.003)
        end_idle = (mode == MODE_IDLE) & ~idler_stay & (u < 0.05)
        to_park = ((mode == MODE_DRIVING) & (u > 0.985)) | end_idle
        newmode = mode.copy()
        newmode[to_drive] = MODE_DRIVING
        newmode[to_idle] = MODE_IDLE
        newmode[to_park] = MODE_PARKED
        started = to_drive & (mode == MODE_PARKED)
        self.trip_no[i[started]] += 1
        mode = newmode
        self.mode[i] = mode
        driving, idle = mode == MODE_DRIVING, mode == MODE_IDLE

        # ---- kinematics
        speed = self.speed[i]
        target = np.where(driving, self.limit[i] * r.uniform(0.55, 1.0, k), 0.0)
        aggressive = prof == PROFILE_AGGRESSIVE
        target = np.where(driving & aggressive, self.limit[i] * r.uniform(1.0, 1.35, k), target)
        speed = np.clip(speed + (target - speed) * np.minimum(1.0, 0.25 * dt) + r.normal(0, 2.0, k) * driving, 0, 190)
        speed = np.where(driving, speed, 0.0)
        accel = (speed - self.speed[i]) / dt  # km/h per second
        self.speed[i] = speed
        h = self.heading[i] + r.normal(0, 0.15, k)
        # steer home when far from the depot
        dlat, dlon = self.lat[i] - self.home_lat[i], self.lon[i] - self.home_lon[i]
        far = np.hypot(dlat, dlon) > 0.35
        h = np.where(far, np.arctan2(-dlat, -dlon * math.cos(math.radians(45))) , h)
        self.heading[i] = h
        km = speed * dt / 3600.0
        self.lat[i] += km / 111.0 * np.sin(h)
        self.lon[i] += km / (111.0 * np.cos(np.radians(self.lat[i]))) * np.cos(h)
        self.odo[i] += km

        # ---- engine / battery
        ev = fuel == 2
        rpm = np.where(driving, 900 + speed * 28 + r.normal(0, 80, k), np.where(idle, 780 + r.normal(0, 40, k), 0.0))
        rpm = np.where(ev, 0.0, np.clip(rpm, 0, 6500))
        burn = np.where(idle, 0.0008, 0.0015) * dt * np.where(driving, 1 + speed / 80, 1)
        self.fuel_pct[i] = np.where(ev, self.fuel_pct[i], np.maximum(0.0, self.fuel_pct[i] - burn))
        self.fuel_pct[i] = np.where(self.fuel_pct[i] < 6, 100.0, self.fuel_pct[i])  # refuel
        self.soc[i] = np.where(fuel >= 2, np.where(self.soc[i] < 8, 95.0, self.soc[i] - 0.0045 * dt * (0.2 + speed / 60) * driving), self.soc[i])
        target_cool = np.where(driving | idle, 88 + speed * 0.05, 25.0)
        overheat = (prof == PROFILE_OVERHEAT) & (driving | idle)
        target_cool = np.where(overheat, 118 + r.normal(0, 4, k), target_cool)
        self.coolant[i] += (target_cool - self.coolant[i]) * np.minimum(1.0, 0.08 * dt) + r.normal(0, 0.4, k)
        coolant = np.clip(self.coolant[i], -10, 200)
        oil_t = coolant + 8 + r.normal(0, 1, k)
        oil_p = np.where(rpm > 0, 180 + rpm * 0.05 + r.normal(0, 8, k), 0.0)
        oil_p = np.where(overheat & (r.random(k) < 0.15), 60 + r.normal(0, 10, k), oil_p)
        voltage = np.where(rpm > 0, 13.9, 12.6) + r.normal(0, 0.08, k)
        voltage = np.where(ev, 12.2 + r.normal(0, 0.05, k), voltage)
        vib = np.abs(r.normal(0.4, 0.15, k)) + np.where(overheat, r.uniform(0, 2.5, k), 0.0)
        self.bat_temp[i] += ((26 + np.where(driving, 12, 0) + (prof == PROFILE_BATTERY) * 22) - self.bat_temp[i]) * np.minimum(1.0, 0.05 * dt) + r.normal(0, 0.3, k)
        soh = self.soh[i] - np.where(prof == PROFILE_BATTERY, 0.0005 * dt, 0.0)
        self.soh[i] = soh

        # ---- events: harsh driving, speeding, idling
        harsh_p = np.where(aggressive, 0.12, 0.004)
        hb = driving & ((accel < -9.0) | (r.random(k) < harsh_p))
        ha = driving & ((accel > 9.0) | (r.random(k) < harsh_p * 0.8))
        speeding = driving & (speed > self.limit[i] + 5)
        dtc_p = np.where(prof == PROFILE_DTC, 0.08, 0.0008)
        has_dtc = (r.random(k) < dtc_p) & (fuel != 2)
        dtc_pick = r.integers(0, 4, k)
        self.seq[i] += 1

        # ---- assemble (single Python loop; numpy -> python scalars via tolist())
        def L(a: np.ndarray, nd: int = 3) -> list:
            return np.round(a, nd).tolist()

        vins = [self.vins[j] for j in i]
        tenants = [self.tenants[j] for j in i]
        seqs, lats, lons, spds = self.seq[i].tolist(), L(self.lat[i], 6), L(self.lon[i], 6), L(speed, 1)
        odos, rpms, fl, ct, ot, op = L(self.odo[i], 2), L(rpm, 0), L(self.fuel_pct[i], 1), L(coolant, 1), L(oil_t, 1), L(oil_p, 0)
        volts, vibs, socs, sohs, bts = L(voltage, 2), L(vib, 2), L(self.soc[i], 1), L(soh, 2), L(self.bat_temp[i], 1)
        fuels, hbs, has_, sps, idles, drv, dtcs, picks = fuel.tolist(), hb.tolist(), ha.tolist(), speeding.tolist(), idle.tolist(), driving.tolist(), has_dtc.tolist(), dtc_pick.tolist()
        trips, modes = self.trip_no[i].tolist(), mode.tolist()
        codes = ("P0301", "P0171", "P0420", "P0300")
        out: list[dict] = []
        for n_ in range(k):
            ft = fuels[n_]
            e = {"schema_version": 1, "event_id": f"{vins[n_]}-{seqs[n_]}", "tenant": tenants[n_], "vin": vins[n_], "seq": seqs[n_],
                 "ts": now_ms, "lat": lats[n_], "lon": lons[n_], "speed_kmh": spds[n_], "odometer_km": odos[n_],
                 "fuel_type": FUEL_NAMES[ft], "harsh_brake": hbs[n_], "harsh_accel": has_[n_], "idling": idles[n_],
                 "speeding": sps[n_], "engine_on": modes[n_] != MODE_PARKED or ft == 2 and drv[n_],
                 "voltage_v": volts[n_], "dtc": [codes[picks[n_]]] if dtcs[n_] else [],
                 "trip_id": f"{vins[n_]}-t{trips[n_]}" if modes[n_] != MODE_PARKED else None}
            if ft != 2:
                e.update(rpm=rpms[n_], fuel_level_pct=fl[n_], coolant_temp_c=ct[n_], oil_temp_c=ot[n_], oil_pressure_kpa=op[n_], vibration_g=vibs[n_])
            if ft >= 2:
                e.update(battery_soc_pct=socs[n_], battery_soh_pct=sohs[n_], battery_temp_c=bts[n_])
            out.append(e)
        return out
