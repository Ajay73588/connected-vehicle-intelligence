"""Redis: latest vehicle state (hot), geohash cells for the live map, atomic dedupe+aggregates, rate limiting, cache."""

from __future__ import annotations

import time
from collections.abc import Iterable

import orjson
import redis

from cvi.algorithms.geo import geohash_encode, geohash_neighbors
from cvi.common.config import get_settings

CELL_PRECISION = 4  # ~39 km x 20 km cells; the map queries a handful of cells per viewport

# Atomic "claim event id + apply aggregates": SADD is the idempotency gate, so each event contributes to the
# aggregates exactly once even under at-least-once delivery and consumer restarts.
_COMMIT_LUA = """
if redis.call('SADD', KEYS[1], ARGV[1]) == 0 then return 0 end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
for i = 5, #ARGV, 2 do
  redis.call('HINCRBYFLOAT', KEYS[2], ARGV[i], ARGV[i+1])
  redis.call('HINCRBYFLOAT', KEYS[3], ARGV[i], ARGV[i+1])
end
redis.call('EXPIRE', KEYS[2], 172800)
redis.call('EXPIRE', KEYS[3], 259200)
redis.call('PFADD', KEYS[4], ARGV[3])
redis.call('EXPIRE', KEYS[4], 172800)
return 1
"""

_RATE_LUA = """
local t = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local now = tonumber(ARGV[1]); local rate = tonumber(ARGV[2]); local cap = tonumber(ARGV[3])
local tokens = tonumber(t[1]); local ts = tonumber(t[2])
if tokens == nil then tokens = cap; ts = now end
tokens = math.min(cap, tokens + (now - ts) * rate)
local ok = 0
if tokens >= 1 then tokens = tokens - 1; ok = 1 end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], 120)
return ok
"""


_FAKE_SERVER = None


def get_redis(url: str | None = None) -> redis.Redis:
    """`fakeredis://` selects an in-process Redis emulator: ONLY for the embedded single-process dev mode and unit tests."""
    global _FAKE_SERVER
    u = url or get_settings().redis_url
    if u.startswith("fakeredis://"):
        import fakeredis

        if _FAKE_SERVER is None:
            _FAKE_SERVER = fakeredis.FakeServer()
        return fakeredis.FakeRedis(server=_FAKE_SERVER, decode_responses=True)
    return redis.Redis.from_url(u, protocol=2, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)


def hour_key(ts_ms: int) -> str:
    return time.strftime("%Y%m%d%H", time.gmtime(ts_ms / 1000))


def day_key(ts_ms: int) -> str:
    return time.strftime("%Y%m%d", time.gmtime(ts_ms / 1000))


class StateStore:
    def __init__(self, client: redis.Redis | None = None):
        self.r = client or get_redis()
        self._commit = self.r.register_script(_COMMIT_LUA)
        self._rate = self.r.register_script(_RATE_LUA)

    def ping(self) -> bool:
        try:
            return bool(self.r.ping())
        except redis.RedisError:
            return False

    # ---- dedupe check (read-only) and atomic commit -------------------------------------------------------
    def seen_many(self, events: list[dict]) -> list[bool]:
        pipe = self.r.pipeline(transaction=False)
        for e in events:
            pipe.sismember(f"seen:{e['vin']}:{hour_key(e['ts'])}", e["event_id"])
        return [bool(x) for x in pipe.execute()]

    def commit_many(self, items: Iterable[tuple[dict, dict[str, float]]]) -> int:
        """items = (event, aggregate increments). Returns how many were newly committed."""
        pipe = self.r.pipeline(transaction=False)
        n = 0
        for e, incs in items:
            hk, dk = hour_key(e["ts"]), day_key(e["ts"])
            args = [e["event_id"], 10800, e["vin"], dk]
            for k, v in incs.items():
                args += [k, v]
            self._commit(keys=[f"seen:{e['vin']}:{hk}", f"agg:{e['tenant']}:{hk}", f"vday:{e['vin']}:{dk}",
                               f"hll:{e['tenant']}:{hk}"], args=args, client=pipe)
            n += 1
        if n == 0:
            return 0
        return sum(int(x) for x in pipe.execute())

    # ---- latest vehicle state + map cells ---------------------------------------------------------------
    STATE_FIELDS = ("tenant", "vin", "ts", "seq", "lat", "lon", "speed_kmh", "rpm", "odometer_km", "fuel_type", "fuel_level_pct",
                    "coolant_temp_c", "oil_temp_c", "oil_pressure_kpa", "voltage_v", "vibration_g", "battery_soc_pct",
                    "battery_soh_pct", "battery_temp_c", "harsh_brake", "harsh_accel", "idling", "speeding", "engine_on")

    def put_states(self, events: list[dict], extra: dict[str, dict] | None = None) -> dict[str, str]:
        """Write the latest state per vehicle (caller guarantees these are the newest events) as ONE compact JSON value
        (a single small Redis argument instead of ~50 hash arguments: client-side serialisation dominated the profile)
        and maintain geohash map cells only when a vehicle changes cell (or every ~10 min to refresh the TTL).
        Returns {vin: cell key}."""
        pipe = self.r.pipeline(transaction=False)
        now = time.time()
        cells: dict[str, str] = {}
        active: dict[str, dict[str, float]] = {}
        for e in events:
            vin = e["vin"]
            ex = (extra or {}).get(vin, {})
            cell = f"cell:{e['tenant']}:{geohash_encode(e['lat'], e['lon'], CELL_PRECISION)}"
            cells[vin] = cell
            old = ex.get("old_cell")
            if old != cell or ex.get("refresh_cell"):
                if old and old != cell:
                    pipe.srem(old, vin)
                pipe.sadd(cell, vin)
                pipe.expire(cell, 3600)
            doc = {k: e[k] for k in self.STATE_FIELDS if e.get(k) is not None}
            doc["dtc"] = ",".join(e.get("dtc") or [])
            doc["cell"] = cell
            for k in ("health", "risk"):
                if k in ex:
                    doc[k] = ex[k]
            pipe.set(f"vstate:{vin}", orjson.dumps(doc), ex=86400)
            active.setdefault(e["tenant"], {})[vin] = now
        for tenant, members in active.items():
            pipe.zadd(f"active:{tenant}", members)
        pipe.execute()
        return cells

    def get_state(self, vin: str) -> dict | None:
        raw = self.r.get(f"vstate:{vin}")
        return orjson.loads(raw) if raw else None

    def get_states(self, vins: list[str]) -> list[dict]:
        pipe = self.r.pipeline(transaction=False)
        for v in vins:
            pipe.get(f"vstate:{v}")
        return [orjson.loads(x) for x in pipe.execute() if x]

    def vins_in_cells(self, tenant: str, cells: list[str], limit: int = 2000) -> list[str]:
        pipe = self.r.pipeline(transaction=False)
        for c in cells:
            pipe.smembers(f"cell:{tenant}:{c}")
        out: list[str] = []
        for s in pipe.execute():
            out.extend(s)
            if len(out) >= limit:
                break
        return out[:limit]

    def cells_for_bbox(self, lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> list[str]:
        """Enumerate geohash cells covering a bbox by walking the cell grid with neighbour lookups (bounded)."""
        start = geohash_encode((lat_min + lat_max) / 2, (lon_min + lon_max) / 2, CELL_PRECISION)
        seen, frontier = {start}, [start]
        from cvi.algorithms.geo import geohash_decode_bbox

        for _ in range(6):  # at most 13x13 cells
            nxt = []
            for gh in frontier:
                for nb in geohash_neighbors(gh):
                    if nb in seen:
                        continue
                    a, b, c, d = geohash_decode_bbox(nb)
                    if b >= lat_min and a <= lat_max and d >= lon_min and c <= lon_max:
                        seen.add(nb)
                        nxt.append(nb)
            frontier = nxt
            if not frontier:
                break
        return list(seen)

    def active_count(self, tenant: str, within_s: int = 300) -> int:
        cutoff = time.time() - within_s
        self.r.zremrangebyscore(f"active:{tenant}", "-inf", cutoff - 3600)
        return int(self.r.zcount(f"active:{tenant}", cutoff, "+inf"))

    def last_state_ts(self, vin: str) -> int | None:
        st = self.get_state(vin)
        return int(st["ts"]) if st else None

    # ---- aggregates --------------------------------------------------------------------------------------
    def hourly_aggregates(self, tenant: str, hours: list[str]) -> dict[str, dict[str, float]]:
        pipe = self.r.pipeline(transaction=False)
        for h in hours:
            pipe.hgetall(f"agg:{tenant}:{h}")
            pipe.pfcount(f"hll:{tenant}:{h}")
        res = pipe.execute()
        out = {}
        for i, h in enumerate(hours):
            d = {k: float(v) for k, v in res[2 * i].items()}
            d["active_vehicles"] = float(res[2 * i + 1])
            out[h] = d
        return out

    def vehicle_days(self, pairs: list[tuple[str, str]]) -> list[dict[str, float]]:
        pipe = self.r.pipeline(transaction=False)
        for vin, day in pairs:
            pipe.hgetall(f"vday:{vin}:{day}")
        return [{str(k): float(v) for k, v in h.items()} for h in pipe.execute()]

    def vehicle_day(self, vin: str, day: str) -> dict[str, float]:
        return {str(k): float(v) for k, v in self.r.hgetall(f"vday:{vin}:{day}").items()}

    # ---- cooldown (alert de-duplication window) ---------------------------------------------------------
    def claim_cooldown(self, vin: str, alert_type: str, ttl_s: int) -> bool:
        return bool(self.r.set(f"cool:{vin}:{alert_type}", 1, nx=True, ex=ttl_s))

    # ---- rate limiter (token bucket, atomic in Redis; shared by all API replicas) ------------------------
    def allow(self, key: str, per_minute: int) -> bool:
        return bool(self._rate(keys=[f"rl:{key}"], args=[time.time(), per_minute / 60.0, per_minute]))

    # ---- cache with stampede protection ---------------------------------------------------------------
    def cached(self, key: str, ttl_s: float, compute):
        raw = self.r.get(f"cache:{key}")
        if raw is not None:
            return orjson.loads(raw)
        got_lock = self.r.set(f"lock:{key}", 1, nx=True, px=3000)
        if not got_lock:  # someone else is computing; wait briefly for their result
            for _ in range(20):
                time.sleep(0.05)
                raw = self.r.get(f"cache:{key}")
                if raw is not None:
                    return orjson.loads(raw)
        try:
            val = compute()
            self.r.set(f"cache:{key}", orjson.dumps(val, default=str), px=int(ttl_s * 1000))
            return val
        finally:
            if got_lock:
                self.r.delete(f"lock:{key}")

    def delete_vehicle(self, vin: str) -> int:
        """GDPR erasure helper: drop all hot-state keys for a vehicle."""
        n = 0
        st = self.get_state(vin)
        if st and st.get("cell"):
            self.r.srem(st["cell"], vin)
        for pat in (f"vstate:{vin}", f"vday:{vin}:*", f"seen:{vin}:*", f"cool:{vin}:*"):
            for k in self.r.scan_iter(match=pat, count=500):
                n += self.r.delete(k)
        return n
