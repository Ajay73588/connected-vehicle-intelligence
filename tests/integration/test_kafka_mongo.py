"""Real Kafka and MongoDB integration tests. They run in CI (service containers: TEST_KAFKA_BOOTSTRAP, TEST_MONGO_URL) and
locally when those services exist; otherwise they are skipped (never faked)."""

import os
import time
import uuid

import pytest
from helpers import VIN, event

pytestmark = pytest.mark.integration

KAFKA = os.environ.get("TEST_KAFKA_BOOTSTRAP")
MONGO = os.environ.get("TEST_MONGO_URL")


@pytest.mark.skipif(not KAFKA, reason="TEST_KAFKA_BOOTSTRAP not set (no Kafka)")
def test_kafka_keyed_partitioning_commit_and_redelivery():
    from cvi.messaging.kafka import KafkaBroker

    b = KafkaBroker(KAFKA, partitions=4)
    b.ensure_topics()
    assert b.healthy()
    group = f"it-{uuid.uuid4().hex[:8]}"
    topic = "telemetry.raw"
    marker = uuid.uuid4().hex
    for i in range(60):
        b.produce(topic, f"vin{i % 3}", f"{marker}:{i}".encode(), {"ingest_ts": "1"})
    assert b.flush(10) == 0

    c1 = b.subscribe(group, [topic])
    got, deadline = [], time.time() + 30
    while len([m for m in got if m.value.startswith(marker.encode())]) < 60 and time.time() < deadline:
        got += c1.poll_batch(500, 1.0)
    mine = [m for m in got if m.value.startswith(marker.encode())]
    assert len(mine) == 60
    per_key: dict[str, list[int]] = {}
    for m in mine:
        per_key.setdefault(m.key, []).append(int(m.value.split(b":")[1]))
    assert all(v == sorted(v) for v in per_key.values())  # per-key ordering
    assert all(len({m.partition for m in mine if m.key == k}) == 1 for k in per_key)  # same key -> same partition
    assert all(m.headers.get("ingest_ts") == "1" for m in mine)
    c1.commit(mine)
    c1.close()

    c2 = b.subscribe(group, [topic])  # committed -> nothing of ours is redelivered
    again = []
    for _ in range(5):
        again += [m for m in c2.poll_batch(500, 1.0) if m.value.startswith(marker.encode())]
    assert again == []
    c2.close()


@pytest.mark.skipif(not MONGO, reason="TEST_MONGO_URL not set (no MongoDB)")
def test_mongo_raw_store_is_idempotent_with_ttl_index():
    from cvi.storage.mongo_store import RawEventStore, get_mongo

    db = get_mongo(MONGO, f"cvi_test_{uuid.uuid4().hex[:6]}")
    store = RawEventStore(db, ttl_days=7)
    store.ensure_indexes()
    now = int(time.time() * 1000)
    evs = [event(i, now + i) for i in range(50)]
    store.upsert_many(evs)
    store.upsert_many(evs)
    assert store.events.count_documents({}) == 50
    assert [e["seq"] for e in store.recent(VIN, limit=3)] == [49, 48, 47]
    ttl = [i for i in store.events.list_indexes() if i["name"] == "ttl_hot_tier"][0]
    assert ttl["expireAfterSeconds"] == 7 * 86400
    assert store.delete_vehicle(VIN) == 50
    db.client.drop_database(db.name)
