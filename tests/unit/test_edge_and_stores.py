import io
import time

import mongomock
import orjson
import pytest
from fastapi.testclient import TestClient
from helpers import VIN, event

from cvi.common.config import get_settings
from cvi.ingestion.gateway import create_edge_app
from cvi.messaging.base import TOPIC_RAW
from cvi.messaging.memory import MemoryBroker
from cvi.simulator.sinks import MemorySink, NullSink, StdoutSink, make_sink
from cvi.storage.mongo_store import MemoryRawStore, RawEventStore
from cvi.storage.object_store import LocalObjectStore


@pytest.fixture()
def edge():
    b = MemoryBroker(4)
    b.ensure_topics()
    return TestClient(create_edge_app(b)), b


def key():
    return {"X-Device-Key": get_settings().ingest_api_key}


def test_edge_requires_device_key_and_accepts_batches(edge):
    c, b = edge
    batch = [event(i) for i in range(1, 4)]
    assert c.post("/v1/telemetry", json=batch).status_code == 401
    assert c.post("/v1/telemetry", json=batch, headers={"X-Device-Key": "wrong"}).status_code == 401
    r = c.post("/v1/telemetry", json=batch, headers=key())
    assert r.status_code == 202 and r.json() == {"accepted": 3}
    msgs = b.messages(TOPIC_RAW)
    assert len(msgs) == 3 and all(m.headers["ingest_ts"] for m in msgs) and {m.key for m in msgs} == {VIN}


def test_edge_accepts_garbage_for_dlq_but_rejects_oversized_and_non_json(edge):
    c, b = edge
    assert c.post("/v1/telemetry", content=b"not json", headers=key()).status_code == 400
    assert c.post("/v1/telemetry", json=[event(i) for i in range(5001)], headers=key()).status_code == 413
    assert c.post("/v1/telemetry", json=[{"junk": 1}], headers=key()).status_code == 202  # validation (and DLQ) is the next stage's job


def test_edge_applies_back_pressure_when_broker_is_down(edge):
    c, b = edge
    b.down = True
    r = c.post("/v1/telemetry", json=[event(1)], headers=key())
    assert r.status_code == 503 and r.headers["retry-after"] == "2"
    assert c.get("/health/live").status_code == 200  # liveness is independent of dependencies
    assert c.get("/health/ready").status_code == 503  # readiness reflects the broker
    b.down = False
    assert c.get("/health/ready").status_code == 200
    assert c.post("/v1/telemetry", json=[event(2)], headers=key()).status_code == 202  # recovers by itself
    assert b"events_received_total" in c.get("/metrics").content


def test_raw_event_store_upserts_by_event_id_and_serves_recent():
    store = RawEventStore(mongomock.MongoClient()["t"], ttl_days=7)
    store.ensure_indexes()
    now = int(time.time() * 1000)
    evs = [event(i, now + i * 1000) for i in range(5)]
    store.upsert_many(evs)
    store.upsert_many(evs)  # replay -> no duplicates
    assert store.events.count_documents({}) == 5
    recent = store.recent(VIN, limit=3)
    assert [e["seq"] for e in recent] == [4, 3, 2] and recent[0]["event_id"] == evs[4]["event_id"]
    assert store.recent(VIN, since_ts=now + 3000)[0]["seq"] == 4 and len(store.recent(VIN, since_ts=now + 3000)) == 2
    assert store.ping() and store.delete_vehicle(VIN) == 5 and store.recent(VIN) == []
    assert any(i["name"] == "ttl_hot_tier" for i in store.events.list_indexes())
    store.put_dlq({"reason": "x"})
    assert store.dlq.count_documents({}) == 1


def test_memory_raw_store_mirrors_the_interface():
    s = MemoryRawStore(max_per_vin=3)
    s.upsert_many([event(i, 1_700_000_000_000 + i) for i in range(6)])
    assert [e["seq"] for e in s.recent(VIN)] == [5, 4, 3]
    assert s.delete_vehicle(VIN) == 3


def test_local_object_store_rejects_path_traversal(tmp_path):
    st = LocalObjectStore(str(tmp_path))
    st.put("a/b.bin", b"x")
    assert st.get("a/b.bin") == b"x" and st.exists("a/b.bin") and st.list("a/") == ["a/b.bin"]
    with pytest.raises(ValueError):
        st.get("../../etc/passwd")
    st.delete("a/b.bin")
    assert not st.exists("a/b.bin")


def test_simulator_sinks(monkeypatch, capsysbinary):
    m = MemorySink()
    m.send([("k", b"1")])
    assert m.msgs == [("k", b"1")]
    n = NullSink()
    n.send([("k", b"1")] * 3)
    assert n.count == 3
    StdoutSink().send([("k", b'{"a":1}')])
    assert capsysbinary.readouterr().out == b'{"a":1}\n'
    assert isinstance(make_sink("null"), NullSink)
    with pytest.raises(ValueError):
        make_sink("carrier-pigeon")
    _ = io, orjson
