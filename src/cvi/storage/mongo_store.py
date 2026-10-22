"""MongoDB: schemaless HOT store for raw telemetry documents (7-day TTL) and dead-letter documents.

Why a document store: telemetry signals vary by vehicle type (EV vs ICE vs hybrid) and firmware version, arrive at
very high write rates, are append-only and are read by (vehicle, time range). A document model with a TTL index and a
hashed shard key {tenant, vin} fits better than wide nullable relational columns.
Idempotency: `_id` is the event id, so upserts make replays harmless."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient, ReplaceOne

from cvi.common.config import get_settings

_MOCK_CLIENT: Any = None


def get_mongo(url: str | None = None, db: str | None = None):
    """`mongomock://` selects an in-process emulator: ONLY for the embedded single-process dev mode and unit tests."""
    global _MOCK_CLIENT
    s = get_settings()
    if (url or s.mongo_url).startswith("mongomock://"):
        import mongomock

        if _MOCK_CLIENT is None:
            _MOCK_CLIENT = mongomock.MongoClient()
        return _MOCK_CLIENT[db or s.mongo_db]
    client: Any = MongoClient(url or s.mongo_url, serverSelectionTimeoutMS=2000, retryWrites=True)
    return client[db or s.mongo_db]


class RawEventStore:
    def __init__(self, database: Any | None = None, ttl_days: int | None = None):
        self.db = database if database is not None else get_mongo()
        self.events = self.db["events"]
        self.dlq = self.db["dlq"]
        self.ttl_s = (ttl_days or get_settings().raw_events_ttl_days) * 86400

    def ensure_indexes(self) -> None:
        self.events.create_index([("vin", ASCENDING), ("ts", DESCENDING)], name="vin_ts")
        self.events.create_index([("tenant", ASCENDING), ("ts", DESCENDING)], name="tenant_ts")
        self.events.create_index([("ingested_at", ASCENDING)], expireAfterSeconds=self.ttl_s, name="ttl_hot_tier")
        self.dlq.create_index([("created_at", ASCENDING)], expireAfterSeconds=14 * 86400, name="ttl_dlq")

    def ping(self) -> bool:
        try:
            self.db.command("ping")
            return True
        except Exception:
            return False

    def upsert_many(self, events: list[dict]) -> int:
        if not events:
            return 0
        now = dt.datetime.now(dt.UTC)
        ops = [ReplaceOne({"_id": e["event_id"]}, {**{k: v for k, v in e.items() if k != "event_id"}, "ingested_at": now}, upsert=True)
               for e in events]
        try:
            res = self.events.bulk_write(ops, ordered=False)
        except TypeError:  # mongomock (embedded dev mode) does not understand newer ReplaceOne options
            n = 0
            for e in events:
                doc = {k: v for k, v in e.items() if k != "event_id"}
                n += self.events.replace_one({"_id": e["event_id"]}, {**doc, "ingested_at": now}, upsert=True).modified_count
            return n
        return res.upserted_count + res.modified_count

    def recent(self, vin: str, limit: int = 200, since_ts: int | None = None) -> list[dict]:
        q: dict = {"vin": vin}
        if since_ts:
            q["ts"] = {"$gte": since_ts}
        docs = list(self.events.find(q).sort("ts", -1).limit(limit))
        for d in docs:
            d["event_id"] = d.pop("_id")
            d.pop("ingested_at", None)
        return docs

    def put_dlq(self, doc: dict) -> None:
        self.dlq.insert_one({**doc, "created_at": dt.datetime.now(dt.UTC)})

    def delete_vehicle(self, vin: str) -> int:
        return self.events.delete_many({"vin": vin}).deleted_count


class MemoryRawStore:
    """Dict-backed stand-in with the RawEventStore interface, used ONLY by the embedded dev mode (MONGO_URL=memory://)."""

    def __init__(self, max_per_vin: int = 2000):
        self._docs: dict[str, dict[str, dict]] = {}
        self._dlq: list[dict] = []
        self.max_per_vin = max_per_vin

    def ensure_indexes(self) -> None: ...

    def ping(self) -> bool:
        return True

    def upsert_many(self, events: list[dict]) -> int:
        for e in events:
            bucket = self._docs.setdefault(e["vin"], {})
            bucket[e["event_id"]] = e
            if len(bucket) > self.max_per_vin:
                bucket.pop(next(iter(bucket)))
        return len(events)

    def recent(self, vin: str, limit: int = 200, since_ts: int | None = None) -> list[dict]:
        docs = [d for d in self._docs.get(vin, {}).values() if not since_ts or d["ts"] >= since_ts]
        return [dict(d) for d in sorted(docs, key=lambda d: d["ts"], reverse=True)[:limit]]

    def put_dlq(self, doc: dict) -> None:
        self._dlq.append(doc)

    def delete_vehicle(self, vin: str) -> int:
        return len(self._docs.pop(vin, {}))


def get_raw_store() -> RawEventStore | MemoryRawStore:
    if get_settings().mongo_url.startswith("memory://"):
        return _memory_singleton()
    return RawEventStore()


_MEM: MemoryRawStore | None = None


def _memory_singleton() -> MemoryRawStore:
    global _MEM
    if _MEM is None:
        _MEM = MemoryRawStore()
    return _MEM
