"""Network/device imperfections applied to a clean stream: duplicates, out-of-order, late, malformed, recovery bursts."""

from __future__ import annotations

import random
from collections import deque

import orjson

from cvi.simulator.engine import ChaosConfig


class ChaosInjector:
    def __init__(self, cfg: ChaosConfig, seed: int = 7, allowed_lateness_s: int = 300):
        self.cfg, self.rnd = cfg, random.Random(seed)
        self.lateness_ms = (allowed_lateness_s + 60) * 1000
        self._held: deque[tuple[int, dict]] = deque()  # (release_tick, event) for out-of-order delivery
        self._backlog: list[dict] = []  # outage buffer, flushed as a recovery burst
        self.tick = 0
        self.stats = {"duplicates": 0, "out_of_order": 0, "late": 0, "malformed": 0, "burst_flushed": 0}

    def apply(self, events: list[dict]) -> list[tuple[str | None, bytes]]:
        """Return wire messages (key, payload)."""
        c, rnd = self.cfg, self.rnd
        self.tick += 1
        out: list[tuple[str | None, bytes]] = []
        released: list[dict] = []
        while self._held and self._held[0][0] <= self.tick:
            released.append(self._held.popleft()[1])

        keep: list[dict] = []
        for e in events:
            u = rnd.random()
            if u < c.out_of_order_rate:  # delivered 1-3 ticks later => arrives after newer events of the same vehicle
                self._held.append((self.tick + rnd.randint(1, 3), e))
                self.stats["out_of_order"] += 1
            elif u < c.out_of_order_rate + c.late_rate:  # older than the allowed lateness
                e = {**e, "ts": e["ts"] - self.lateness_ms, "event_id": e["event_id"] + "-late"}
                self.stats["late"] += 1
                keep.append(e)
            elif c.outage_vehicle_share and rnd.random() < c.outage_vehicle_share:
                self._backlog.append(e)
            else:
                keep.append(e)
        if self._backlog and rnd.random() < 0.05:  # connectivity restored -> recovery burst
            self.stats["burst_flushed"] += len(self._backlog)
            released.extend(self._backlog)
            self._backlog = []

        for e in keep + released:
            payload = orjson.dumps(e)
            out.append((e["vin"], payload))
            if rnd.random() < c.duplicate_rate:
                out.append((e["vin"], payload))
                self.stats["duplicates"] += 1
            if rnd.random() < c.malformed_rate:
                out.append(self._malformed(e, payload))
                self.stats["malformed"] += 1
        return out

    def _malformed(self, e: dict, payload: bytes) -> tuple[str | None, bytes]:
        kind = self.rnd.randrange(6)
        if kind == 0:
            return e["vin"], payload[: len(payload) // 2]  # truncated JSON
        if kind == 1:
            vin = list(e["vin"])
            vin[5] = "A" if vin[5] != "A" else "B"  # breaks the check digit
            return e["vin"], orjson.dumps({**e, "vin": "".join(vin), "event_id": e["event_id"] + "x"})
        if kind == 2:
            return e["vin"], orjson.dumps({**e, "speed_kmh": -50, "event_id": e["event_id"] + "x"})
        if kind == 3:
            d = {k: v for k, v in e.items() if k != "ts"}
            return e["vin"], orjson.dumps(d)
        if kind == 4:
            return e["vin"], orjson.dumps({**e, "lat": "north", "event_id": e["event_id"] + "x"})
        return e["vin"], orjson.dumps({**e, "ts": e["ts"] + 3_600_000, "event_id": e["event_id"] + "x"})  # clock far in the future
