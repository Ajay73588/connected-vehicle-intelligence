"""Stream processor behaviour: idempotency, ordering, DLQ, failure recovery, degradation. Uses the real Processor with
in-memory stores (fakeredis runs the real Lua script); PostgreSQL-backed behaviour is covered in tests/integration."""

import tempfile

import fakeredis
import orjson
import pytest
from helpers import OTHER_VIN, VIN, MemorySink, event, msgs

from cvi.common.config import get_settings
from cvi.messaging.base import TOPIC_ALERTS, TOPIC_DLQ
from cvi.messaging.memory import MemoryBroker
from cvi.storage.mongo_store import MemoryRawStore
from cvi.storage.object_store import LocalObjectStore
from cvi.storage.redis_store import StateStore, day_key
from cvi.stream.processor import Processor, SinkUnavailable
from cvi.stream.worker import Worker


@pytest.fixture()
def rig():
    broker = MemoryBroker(4)
    broker.ensure_topics()
    state = StateStore(fakeredis.FakeRedis(decode_responses=True))
    sink = MemorySink()
    proc = Processor(broker, state, MemoryRawStore(), LocalObjectStore(tempfile.mkdtemp()), sink, parquet_flush_s=0)
    return proc, broker, state, sink


def dlq_reasons(broker):
    return [orjson.loads(m.value)["reason"] for m in broker.messages(TOPIC_DLQ)]


def agg(state, ev):
    return state.vehicle_day(ev["vin"], day_key(ev["ts"]))


def test_duplicate_event_is_processed_once(rig):
    proc, broker, state, _ = rig
    e = event(1)
    res = proc.process_batch(msgs([e, e]))  # same event twice in one batch
    assert res.duplicates == 1 and res.processed == 1
    res2 = proc.process_batch(msgs([e], 10))  # and again in a later batch (replay)
    assert res2.duplicates == 1 and res2.processed == 0
    assert agg(state, e)["n"] == 1.0  # aggregates counted exactly once


def test_replayed_batch_after_crash_does_not_double_count(rig):
    proc, broker, state, sink = rig
    batch = msgs([event(i) for i in range(1, 6)])
    proc.process_batch(batch)
    # crash before offset commit -> a NEW processor instance (empty memory) receives the same batch again
    proc2 = Processor(broker, state, MemoryRawStore(), LocalObjectStore(tempfile.mkdtemp()), sink)
    res = proc2.process_batch(batch)
    assert res.duplicates == 5 and res.processed == 0
    assert agg(state, orjson.loads(batch[0].value))["n"] == 5.0


def test_out_of_order_events_are_windowed_by_event_time(rig):
    proc, _, state, _ = rig
    base = 1_800_000_000_000
    evs = [event(103, base + 3000, harsh_brake=True), event(101, base + 1000, harsh_brake=True), event(102, base + 2000)]
    res = proc.process_batch(msgs(evs))
    # processor orders a batch by event time itself, so nothing is "late"; the latest state is the newest event
    assert res.processed == 3 and res.late == 0
    assert agg(state, evs[0])["hb"] == 2.0
    assert state.get_state(VIN)["seq"] == 103


def test_event_arriving_after_a_newer_one_is_out_of_order_not_dropped(rig):
    proc, _, state, _ = rig
    base = 1_800_000_000_000
    proc.process_batch(msgs([event(10, base + 10_000)]))
    old = event(9, base + 9_000, harsh_brake=True)
    res = proc.process_batch(msgs([old], 5))
    assert res.out_of_order == 1 and res.processed == 1
    assert state.get_state(VIN)["seq"] == 10  # latest state NOT regressed by the old event
    assert agg(state, old)["hb"] == 1.0


def test_event_older_than_allowed_lateness_is_counted_but_not_applied(rig):
    proc, _, state, _ = rig
    base = 1_800_000_000_000
    proc.process_batch(msgs([event(50, base)]))
    late = event(5, base - (get_settings().allowed_lateness_s + 60) * 1000, harsh_brake=True)
    res = proc.process_batch(msgs([late], 3))
    assert res.late == 1
    assert agg(state, late).get("hb", 0.0) == 0.0  # not applied to aggregates
    assert agg(state, late)["late"] == 1.0
    assert proc.raw.recent(VIN, 10)  # but still preserved in the raw store


def test_malformed_unknown_and_spoofed_tenant_go_to_dlq(rig):
    proc, broker, _, _ = rig
    from cvi.messaging.base import TOPIC_VALIDATED, Message

    bad = [Message(TOPIC_VALIDATED, None, b"{not json", 0, 0, {}),
           Message(TOPIC_VALIDATED, None, orjson.dumps({"event_id": "x"}), 0, 1, {}),
           msgs([event(1, vin="1FT" + "A" * 14)])[0],  # not a known vehicle
           msgs([event(2, tenant="borealis-ev")], 3)[0]]  # VIN belongs to acme -> spoofed tenant
    res = proc.process_batch(bad)
    assert res.dlq == 4 and res.processed == 0
    assert sorted(dlq_reasons(broker)) == ["malformed", "malformed", "tenant_mismatch", "unknown_vehicle"]


def test_alert_generated_with_cooldown_and_published(rig):
    proc, broker, _, sink = rig
    base = 1_800_000_000_000
    evs = [event(i, base + i * 15_000, coolant_temp_c=120.0) for i in range(1, 8)]
    res = proc.process_batch(msgs(evs))
    assert res.alerts == 1  # many qualifying events, ONE alert inside the cooldown bucket
    a = next(iter(sink.alerts.values()))
    assert a["type"] == "engine_overheating" and a["severity"] == "critical" and 0 < a["confidence"] <= 1
    assert a["recommended_action"] and a["cause"]
    assert len(broker.messages(TOPIC_ALERTS)) == 1


def test_sink_outage_does_not_lose_alerts_and_replay_is_safe(rig):
    proc, broker, state, sink = rig
    base = 1_800_000_000_000
    evs = [event(i, base + i * 15_000, coolant_temp_c=120.0) for i in range(1, 6)]
    batch = msgs(evs)
    sink.fail_alerts = 1
    with pytest.raises(SinkUnavailable):
        proc.process_batch(batch)
    assert not sink.alerts and agg(state, evs[0]) == {}  # nothing committed (dedupe marks written last)
    res = proc.process_batch(batch)  # retry the SAME batch (worker back-pressure loop)
    assert res.alerts == 1 and len(sink.alerts) == 1
    assert agg(state, evs[0])["n"] == 5.0  # each event counted once despite the failed first attempt


def test_worker_retries_same_batch_until_sink_recovers(rig):
    proc, broker, _, sink = rig
    base = 1_800_000_000_000
    for m in msgs([event(i, base + i * 15_000, coolant_temp_c=120.0) for i in range(1, 6)]):
        broker.produce(m.topic, m.key, m.value, m.headers)
    w = Worker(broker, proc, "g-retry")
    sink.fail_alerts = 2
    batch = w.consumer.poll_batch(100, 0.01)
    import cvi.stream.worker as wk

    wk.time.sleep = lambda s: None  # no real sleeping in the test
    w._process_with_retry(batch)
    w.consumer.commit(batch)
    assert len(sink.alerts) == 1 and w.consumer.lag()["telemetry.validated"] == 0


def test_poison_message_is_bisected_to_dlq(rig):
    proc, broker, _, _ = rig
    w = Worker(broker, proc, "g-poison")
    good = msgs([event(1), event(2), event(3)])
    from cvi.messaging.base import TOPIC_VALIDATED, Message

    poison = Message(TOPIC_VALIDATED, VIN, orjson.dumps(event(4, lat=None)), 0, 9, {"ingest_ts": "x"})  # int("x") in latency -> raises
    original = proc.process_batch

    def flaky(batch):
        if any(m is poison for m in batch):
            raise ValueError("boom")
        return original(batch)

    proc.process_batch = flaky
    w._process_with_retry([*good, poison])
    assert dlq_reasons(broker).count("processing_error") == 1
    assert proc.states[VIN].last_seq == 3  # the healthy messages were still processed


def test_redis_outage_degrades_to_bloom_dedupe_instead_of_failing(rig):
    proc, broker, state, _ = rig

    def boom(*a, **k):
        raise ConnectionError("redis down")

    state.seen_many = boom
    state.commit_many = boom
    state.put_states = boom
    e = event(1)
    r1 = proc.process_batch(msgs([e]))
    assert r1.processed == 1 and "redis-commit" in r1.degraded
    r2 = proc.process_batch(msgs([e], 5))  # duplicate is still caught by the in-process Bloom filter
    assert r2.duplicates == 1 and r2.processed == 0


def test_object_store_outage_is_buffered_not_fatal(rig):
    proc, _, _, _ = rig

    def fail(*a, **k):
        raise OSError("s3 down")

    proc.objects.put = fail
    res = proc.process_batch(msgs([event(1)]))
    assert res.processed == 1 and "object-store" in res.degraded
    assert len(proc._pq_buf) == 1  # retained for the next flush


def test_mongo_outage_applies_back_pressure(rig):
    proc, _, _, _ = rig

    def fail(events):
        raise ConnectionError("mongo down")

    proc.raw.upsert_many = fail
    with pytest.raises(SinkUnavailable):
        proc.process_batch(msgs([event(1)]))


def test_events_of_different_tenants_never_mix(rig):
    proc, _, state, _ = rig
    proc.process_batch(msgs([event(1), event(1, vin=OTHER_VIN, tenant="borealis-ev", fuel_type="ev", rpm=None, fuel_level_pct=None,
                                          coolant_temp_c=None, oil_temp_c=None, oil_pressure_kpa=None, vibration_g=None)]))
    assert state.get_state(VIN)["tenant"] == "acme-logistics"
    assert state.get_state(OTHER_VIN)["tenant"] == "borealis-ev"
    assert state.active_count("acme-logistics") == 1 and state.active_count("borealis-ev") == 1
