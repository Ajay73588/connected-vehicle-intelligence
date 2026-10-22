"""Stream processor core: one batch of Kafka messages in, side effects out.

Order of operations (chosen so a crash at ANY point is repaired by replaying the uncommitted batch):
  1. decode + validate envelope; poison messages -> DLQ                       (idempotent)
  2. resolve vehicle/tenant; reject spoofed tenants -> DLQ                    (idempotent)
  3. raw sinks: MongoDB upsert by event_id + Parquet buffer                   (idempotent: _id / content hash)
  4. read-only duplicate check (Redis seen-set; Bloom filter when Redis is down)
  5. event-time ordering -> detectors -> alerts, health, safety               (in-process ring makes retries safe)
  6. persist alerts (PostgreSQL, UNIQUE dedup_key) + publish to the alerts topic (idempotent)
  7. Redis latest state / map cells
  8. atomic commit of "seen" marks + aggregates in Redis (Lua)                (exactly-once effect)
  9. caller commits Kafka offsets
Delivery semantics: at-least-once delivery, effectively-once effects."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

import orjson

from cvi.algorithms.sketches import BloomFilter
from cvi.common import metrics
from cvi.common.config import get_settings
from cvi.common.resilience import CircuitBreaker, CircuitOpenError
from cvi.messaging.base import TOPIC_ALERTS, TOPIC_DLQ, Broker, Message
from cvi.storage.mongo_store import MemoryRawStore, RawEventStore
from cvi.storage.object_store import ObjectStore, write_events
from cvi.storage.redis_store import StateStore, day_key
from cvi.stream.detectors import DetectorEngine, Geofence, VehicleState

logger = logging.getLogger("stream")


class SinkUnavailable(RuntimeError):
    """A downstream dependency is down: the batch must be retried (back-pressure), not dropped."""


class AlertSink(Protocol):
    def resolve_vehicles(self, vins: list[str]) -> dict[str, tuple[int, int, str]]: ...
    def persist_alerts(self, rows: list[dict]) -> list[dict]: ...
    def upsert_snapshots(self, rows: list[dict]) -> None: ...
    def flush_daily(self, rows: list[dict]) -> None: ...
    def load_geofences(self) -> dict[str, list[Geofence]]: ...


@dataclass
class BatchResult:
    received: int = 0
    processed: int = 0
    duplicates: int = 0
    late: int = 0
    out_of_order: int = 0
    dlq: int = 0
    alerts: int = 0
    degraded: list[str] = field(default_factory=list)


class Processor:
    def __init__(self, broker: Broker, state: StateStore, raw: RawEventStore | MemoryRawStore, objects: ObjectStore, sink: AlertSink,
                 parquet_flush_s: float = 5.0, parquet_flush_rows: int = 50_000):
        self.s = get_settings()
        self.broker, self.state, self.raw, self.objects, self.sink = broker, state, raw, objects, sink
        self.states: dict[str, VehicleState] = {}
        self.engine = DetectorEngine(idle_fuel_lph=self.s.idle_fuel_lph, fuel_price=self.s.fuel_price_per_litre)
        self.directory: dict[str, tuple[int, int, str]] = {}
        self.fallback_seen = BloomFilter(2_000_000, 0.001)  # used only while Redis is unavailable
        self.breakers = {n: CircuitBreaker(n, failure_threshold=3, reset_timeout=5.0) for n in ("redis", "mongo", "postgres", "objects")}
        self._pq_buf: list[dict] = []
        self._pq_last = time.monotonic()
        self._pq_flush_s, self._pq_flush_rows = parquet_flush_s, parquet_flush_rows
        self._persisted_alerts: set[str] = set()  # keys already written (bounded); avoids useless INSERT ... ON CONFLICT
        self._geofence_loaded = 0.0
        self._daily_dirty: set[tuple[str, str]] = set()
        self._last_daily_flush = time.monotonic()
        self._dirty_snapshots: set[str] = set()

    # ------------------------------------------------------------------------------------------------ helpers
    def _dlq(self, m: Message, reason: str, detail: str = "") -> None:
        metrics.dlq_total.labels(reason).inc()
        metrics.events_failed_total.labels("stream", reason).inc()
        payload = {"reason": reason, "detail": detail[:300], "topic": m.topic, "partition": m.partition, "offset": m.offset,
                   "value": m.value[:2000].decode("utf-8", "replace")}
        self.broker.produce(TOPIC_DLQ, m.key, orjson.dumps(payload), {"reason": reason})

    def _guard(self, name: str, fn, *, fatal: bool = True):
        try:
            return self.breakers[name].call(fn)
        except CircuitOpenError as e:
            if fatal:
                raise SinkUnavailable(f"{name} circuit open") from e
            raise
        except Exception as e:
            if fatal:
                raise SinkUnavailable(f"{name}: {e}") from e
            raise

    def _refresh_geofences(self) -> None:
        if time.monotonic() - self._geofence_loaded > 60:
            try:
                self.engine.geofences = self.sink.load_geofences()
                self._geofence_loaded = time.monotonic()
            except Exception as e:
                logger.warning("geofence refresh failed: %s", e)

    # ------------------------------------------------------------------------------------------------ main
    def process_batch(self, messages: list[Message]) -> BatchResult:
        res = BatchResult(received=len(messages))
        now = time.time()
        t0 = time.perf_counter()
        self._refresh_geofences()

        # 1-2: decode, envelope check, vehicle resolution
        decoded: list[tuple[dict, Message]] = []
        for m in messages:
            try:
                e = orjson.loads(m.value)
                if not all(k in e for k in ("event_id", "vin", "tenant", "ts", "lat", "lon")):
                    raise ValueError("missing required keys")
            except Exception as ex:
                self._dlq(m, "malformed", str(ex))
                res.dlq += 1
                continue
            decoded.append((e, m))
        unknown = [e["vin"] for e, _ in decoded if e["vin"] not in self.directory]
        if unknown:
            self.directory.update(self._guard("postgres", lambda: self.sink.resolve_vehicles(sorted(set(unknown)))))
        valid: list[tuple[dict, Message]] = []
        for e, m in decoded:
            info = self.directory.get(e["vin"])
            if info is None:
                self._dlq(m, "unknown_vehicle", e["vin"])
                res.dlq += 1
            elif info[2] != e["tenant"]:
                self._dlq(m, "tenant_mismatch", f"{e['vin']} claims {e['tenant']}")
                res.dlq += 1
            else:
                valid.append((e, m))

        # 3: raw sinks (idempotent)
        events = [e for e, _ in valid]
        if events:
            self._guard("mongo", lambda: self.raw.upsert_many(events))
            self._pq_buf.extend(events)
            self._maybe_flush_parquet(res)

        # 4: duplicate check
        in_batch: set[str] = set()
        candidates: list[tuple[dict, Message]] = []
        for e, m in valid:
            if e["event_id"] in in_batch:
                res.duplicates += 1
                continue
            in_batch.add(e["event_id"])
            candidates.append((e, m))
        seen_flags = self._seen(candidates, res)
        new: list[tuple[dict, Message]] = []
        for (e, m), seen in zip(candidates, seen_flags, strict=True):
            if seen:
                res.duplicates += 1
            else:
                new.append((e, m))
        metrics.events_duplicate_total.inc(res.duplicates)

        # 5: ordering + detection per vehicle, in event-time order
        by_vin: dict[str, list[tuple[dict, Message]]] = defaultdict(list)
        for pair in new:
            by_vin[pair[0]["vin"]].append(pair)
        alert_rows: list[dict] = []
        alert_rows_by_key: dict[str, int] = {}
        newest: dict[str, dict] = {}
        commits: list[tuple[dict, dict[str, float]]] = []
        late_ms = self.s.allowed_lateness_s * 1000
        for vin, pairs in by_vin.items():
            vid, tid, tenant = self.directory[vin]
            st = self.states.get(vin)
            if st is None:
                st = self.states[vin] = VehicleState(vin, tenant)
            pairs.sort(key=lambda p: (p[0]["ts"], p[0].get("seq", 0)))
            for e, m in pairs:
                if st.already_processed(e["event_id"]):
                    # a previous attempt of this batch failed downstream: re-emit the identical outcome (idempotent retry)
                    kind, cands, incs = st.result(e["event_id"])
                elif st.last_ts and st.last_ts - e["ts"] > late_ms:
                    kind, cands, incs = "late", [], {"late": 1.0}
                    st.mark_processed(e["event_id"], (kind, cands, incs))
                else:
                    kind = "in_order" if e["ts"] >= st.last_ts else "out_of_order"
                    cands, incs = self.engine.update(st, e, kind == "in_order")
                    if kind == "in_order":
                        if st.last_seq >= 0 and e.get("seq", 0) > st.last_seq + 1:
                            st.gaps += e["seq"] - st.last_seq - 1
                        st.last_ts, st.last_seq = e["ts"], e.get("seq", st.last_seq)
                    st.dirty = True
                    st.mark_processed(e["event_id"], (kind, cands, incs))
                if kind == "late":
                    res.late += 1
                    metrics.events_late_total.inc()
                    commits.append((e, incs))
                    continue
                if kind == "out_of_order":
                    res.out_of_order += 1
                    metrics.events_out_of_order_total.inc()
                else:
                    newest[vin] = e
                self._dirty_snapshots.add(vin)
                commits.append((e, incs))
                ingest_ms = int(m.headers.get("ingest_ts", e["ts"]))
                for c in cands:
                    bucket = c.ts // (self.s.alert_cooldown_s * 1000)
                    key = f"{vin}:{c.type}:{c.severity}:{bucket}"  # severity in the key: an escalation inside a bucket still alerts
                    if key in self._persisted_alerts or key in alert_rows_by_key:
                        continue  # one alert per vehicle/type/cooldown bucket (DB UNIQUE dedup_key is the final arbiter)
                    alert_rows_by_key[key] = len(alert_rows)
                    alert_rows.append({
                        "tenant_id": tid, "vehicle_id": vid, "vin": vin, "type": c.type, "severity": c.severity,
                        "confidence": round(c.confidence, 3), "cause": c.cause, "recommended_action": c.recommended_action,
                        "details": c.details, "event_ts": c.ts, "ingest_ts": ingest_ms,
                        "dedup_key": key})
        res.processed = len(commits)

        # 6: persist alerts, publish
        if alert_rows:
            inserted = self._guard("postgres", lambda: self.sink.persist_alerts(alert_rows))
            self._persisted_alerts.update(r["dedup_key"] for r in alert_rows)
            if len(self._persisted_alerts) > 200_000:
                self._persisted_alerts.clear()
            for a in inserted:
                metrics.alerts_generated_total.labels(a["type"], a["severity"]).inc()
                metrics.alert_latency.observe(max(0.0, time.time() - a["ingest_ts"] / 1000.0))
                self.broker.produce(TOPIC_ALERTS, a["vin"], orjson.dumps(a, default=str))
            res.alerts = len(inserted)
        snaps = self._snapshot_rows(force={r["vin"] for r in alert_rows})
        if snaps:
            self._guard("postgres", lambda: self.sink.upsert_snapshots(snaps))

        # 7: latest state (only the newest in-order event per vehicle) + map cells
        if newest:
            extra, now_s = {}, time.time()
            for vin in newest:
                st = self.states[vin]
                score, _ = st.health()
                extra[vin] = {"health": score, "risk": st.safety()["risk_score"], "old_cell": st.cell,
                              "refresh_cell": now_s - st.cell_ts > 600}
            try:
                cells = self.breakers["redis"].call(lambda: self.state.put_states(list(newest.values()), extra))
                for vin, cell in cells.items():
                    if extra[vin]["refresh_cell"] or cell != extra[vin]["old_cell"]:
                        self.states[vin].cell_ts = now_s
                    self.states[vin].cell = cell
            except Exception as ex:
                res.degraded.append("redis-state")
                logger.warning("state write skipped: %s", ex)

        # 8: atomic dedupe commit + aggregates
        try:
            self.breakers["redis"].call(lambda: self.state.commit_many(commits))
        except Exception as ex:
            res.degraded.append("redis-commit")
            for e, _ in commits:
                self.fallback_seen.add(e["event_id"])
            logger.warning("redis commit failed, using in-process Bloom filter for dedupe: %s", ex)
        for e, incs in commits:
            if len(incs) > 1 or incs.get("n"):
                self._daily_dirty.add((e["vin"], day_key(e["ts"])))

        # metrics
        metrics.events_processed_total.labels("stream").inc(res.processed)
        for _, m in new[:: max(1, len(new) // 20)]:  # sampled latency (cheap)
            metrics.processing_latency.observe(max(0.0, now - int(m.headers.get("ingest_ts", now * 1000)) / 1000.0))
        metrics.batch_duration.observe(time.perf_counter() - t0)
        self._maybe_flush_daily()
        return res

    # ------------------------------------------------------------------------------------------------ pieces
    def _seen(self, candidates: list[tuple[dict, Message]], res: BatchResult) -> list[bool]:
        if not candidates:
            return []
        try:
            return self.breakers["redis"].call(lambda: self.state.seen_many([e for e, _ in candidates]))
        except Exception as ex:
            res.degraded.append("redis-dedupe")
            logger.warning("redis unavailable, Bloom-filter dedupe: %s", ex)
            return [e["event_id"] in self.fallback_seen for e, _ in candidates]

    def _maybe_flush_parquet(self, res: BatchResult, force: bool = False) -> None:
        due = time.monotonic() - self._pq_last >= self._pq_flush_s or len(self._pq_buf) >= self._pq_flush_rows
        if not (due or force) or not self._pq_buf:
            if len(self._pq_buf) > 4 * self._pq_flush_rows:
                raise SinkUnavailable("object store backlog too large")
            return
        batch = list(self._pq_buf)
        try:
            self.breakers["objects"].call(lambda: write_events(self.objects, batch))
            self._pq_buf.clear()
            self._pq_last = time.monotonic()
        except Exception as ex:  # degrade: keep buffering; back-pressure only if the backlog explodes
            res.degraded.append("object-store")
            logger.warning("parquet flush failed (will retry): %s", ex)
            self._pq_last = time.monotonic()
            if len(self._pq_buf) > 4 * self._pq_flush_rows:
                raise SinkUnavailable("object store backlog too large") from ex

    def flush(self) -> None:
        """Flush buffers (shutdown / tests)."""
        self._maybe_flush_parquet(BatchResult(), force=True)
        self._maybe_flush_daily(force=True)

    def _snapshot_rows(self, force: set[str]) -> list[dict]:
        out, now = [], time.monotonic()
        for vin in list(self._dirty_snapshots):
            st = self.states[vin]
            if vin not in force and now - st.last_flush < self.s.snapshot_interval_s:
                continue
            vid, tid, _ = self.directory[vin]
            score, factors = st.health()
            out.append({"vehicle_id": vid, "tenant_id": tid, "health_score": score, "factors": factors,
                        "signals": st.signals(), "last_event_ts": st.last_ts, **st.safety()})
            st.last_flush = now
            self._dirty_snapshots.discard(vin)
        return out

    def _maybe_flush_daily(self, force: bool = False) -> None:
        if not self._daily_dirty or (not force and time.monotonic() - self._last_daily_flush < 10.0):
            return
        keys = list(self._daily_dirty)
        try:
            rows = []
            for (vin, day), vals in zip(keys, self.state.vehicle_days(keys), strict=True):
                vid, tid, _ = self.directory[vin]
                rows.append({"vehicle_id": vid, "tenant_id": tid, "day": f"{day[:4]}-{day[4:6]}-{day[6:]}",
                             "events": int(vals.get("n", 0)), "distance_km": vals.get("dist_km", 0.0),
                             "drive_seconds": int(vals.get("drive_s", 0)), "idle_seconds": int(vals.get("idle_s", 0)),
                             "fuel_litres": vals.get("fuel_l", 0.0), "energy_kwh": vals.get("kwh", 0.0),
                             "harsh_brake": int(vals.get("hb", 0)), "harsh_accel": int(vals.get("ha", 0)),
                             "speeding": int(vals.get("sp", 0))})
            self.sink.flush_daily(rows)  # absolute values -> idempotent upsert
            self._daily_dirty.difference_update(keys)
            self._last_daily_flush = time.monotonic()
        except Exception as ex:
            logger.warning("daily stats flush failed (will retry): %s", ex)
