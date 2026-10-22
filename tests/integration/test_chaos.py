"""Failure injection with recovery assertions: database outage, broker outage, consumer crash, API instance death."""

import random
import threading
import time

import orjson
import psycopg
import pytest
from conftest import login
from fastapi.testclient import TestClient
from helpers import pump

from cvi.messaging.base import TOPIC_RAW, TOPIC_VALIDATED
from cvi.simulator.scenarios import SCENARIOS
from cvi.storage.pg import get_pool
from cvi.stream.pg_sink import PgSink
from cvi.stream.processor import Processor
from cvi.stream.worker import Worker

pytestmark = pytest.mark.integration


def kill_db_access(admin_url: str, role: str, allow: bool):
    """Make PostgreSQL unreachable for a role: NOLOGIN + terminate its sessions (what a crash/failover looks like to the app)."""
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(f"ALTER ROLE {role} {'LOGIN' if allow else 'NOLOGIN'}")
        if not allow:
            c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", [role])


def test_database_outage_applies_back_pressure_then_recovers_without_loss_or_duplicates(ctx, seeded_db, monkeypatch):
    import cvi.stream.worker as wk

    real_sleep = time.sleep
    monkeypatch.setattr(wk.time, "sleep", lambda s: real_sleep(min(s, 0.2)))  # shorten back-off for the test
    pool = get_pool(seeded_db["worker"], min_size=1, max_size=3)
    proc = Processor(ctx.broker, ctx.state, ctx.raw, ctx.objects, PgSink(pool), parquet_flush_s=0)
    now = int(time.time() * 1000)
    evs = SCENARIOS["engine_failure"](now, 5150, random.Random(1))
    for e in evs:
        ctx.broker.produce(TOPIC_VALIDATED, e["vin"], orjson.dumps(e), {"ingest_ts": str(now)})
    w = Worker(ctx.broker, proc, "g-chaos-db")
    batch = w.consumer.poll_batch(500, 0.01)
    assert len(batch) == 40

    kill_db_access(seeded_db["admin"], "cvi_worker", allow=False)
    pool.close()  # the app's pooled sessions died with the server
    pool2 = get_pool(seeded_db["worker"] + "?connect_timeout=1", min_size=1, max_size=3)
    proc.sink = PgSink(pool2)
    proc.directory.clear()  # force a vehicle lookup (hits the dead database)
    t = threading.Thread(target=w._process_with_retry, args=(batch,), daemon=True)
    t.start()
    time.sleep(1.5)
    assert t.is_alive()  # still retrying (back-pressure), has NOT given up or dropped the batch
    assert w.consumer.lag()[TOPIC_VALIDATED] == 40  # offsets NOT committed while the sink is down

    kill_db_access(seeded_db["admin"], "cvi_worker", allow=True)  # database comes back
    t.join(timeout=60)
    assert not t.is_alive()
    w.consumer.commit(batch)
    assert w.consumer.lag()[TOPIC_VALIDATED] == 0
    with psycopg.connect(seeded_db["admin"]) as c:
        n = c.execute("SELECT count(*) FROM alerts WHERE dedup_key LIKE %s", [f"{evs[0]['vin']}:engine_overheating:%"]).fetchone()[0]
    assert n >= 1  # the alert produced during the outage window was delivered after recovery
    day = time.strftime("%Y%m%d", time.gmtime(now / 1000))
    assert ctx.state.vehicle_day(evs[0]["vin"], day)["n"] == 40.0  # each event counted exactly once


def test_validator_survives_a_broker_outage_and_resumes(ctx):
    from cvi.ingestion.validator import Validator

    v = Validator(ctx.broker, "validator-chaos")
    th = threading.Thread(target=v.run, daemon=True)
    now = int(time.time() * 1000)
    ctx.broker.down = True
    th.start()
    time.sleep(0.6)
    assert th.is_alive()  # outage did not crash the consumer loop
    ctx.broker.down = False
    for e in SCENARIOS["suspicious_movement"](now, 6, random.Random(1)):
        ctx.broker.produce(TOPIC_RAW, e["vin"], orjson.dumps(e), {"ingest_ts": str(now)})
    deadline = time.time() + 10
    while time.time() < deadline and ctx.broker.depth(TOPIC_VALIDATED) < 12:
        time.sleep(0.1)
    v.running.clear()
    th.join(timeout=5)
    assert ctx.broker.depth(TOPIC_VALIDATED) == 12  # everything produced after the outage was validated


def test_consumer_crash_mid_batch_then_restart_is_exactly_once_in_effect(ctx, seeded_db):
    """A consumer processes a batch, 'crashes' before committing offsets; a fresh consumer re-reads and re-processes the same batch."""
    now = int(time.time() * 1000)
    evs = SCENARIOS["dangerous_driver"](now, 6161, random.Random(3))
    for e in evs:
        ctx.broker.produce(TOPIC_VALIDATED, e["vin"], orjson.dumps(e), {"ingest_ts": str(now)})
    pool = get_pool(seeded_db["worker"])
    c1 = ctx.broker.subscribe("g-crash", [TOPIC_VALIDATED])
    b1 = c1.poll_batch(500, 0)
    Processor(ctx.broker, ctx.state, ctx.raw, ctx.objects, PgSink(pool)).process_batch(b1)
    # crash: no commit, process state lost. Restart:
    c2 = ctx.broker.subscribe("g-crash", [TOPIC_VALIDATED])
    b2 = c2.poll_batch(500, 0)
    assert len(b2) == len(b1) == 60
    res = Processor(ctx.broker, ctx.state, ctx.raw, ctx.objects, PgSink(pool)).process_batch(b2)
    assert res.duplicates == 60 and res.processed == 0
    c2.commit(b2)
    assert ctx.state.vehicle_day(evs[0]["vin"], time.strftime("%Y%m%d", time.gmtime(now / 1000)))["n"] == 60.0
    with psycopg.connect(seeded_db["admin"]) as c:
        keys = [r[0] for r in c.execute("SELECT dedup_key FROM alerts WHERE dedup_key LIKE %s", [f"{evs[0]['vin']}:dangerous_driving:%"])]
    assert len(keys) == len(set(keys))


def test_api_instance_death_is_invisible_to_clients(ctx):
    """Stateless API: tokens issued by instance A are honoured by instance B (shared secret, shared Redis for revocation)."""
    from cvi.api.app import create_app

    a, b = TestClient(create_app()), TestClient(create_app())
    h = login(a, "fleet_manager@acme.demo")
    refresh = a.post("/auth/login", json={"email": "viewer@acme.demo", "password": "Demo@12345"}).json()["refresh_token"]
    assert a.get("/api/v1/vehicles", params={"limit": 5}, headers=h).status_code == 200
    a.close()
    del a  # instance A is gone
    assert b.get("/api/v1/vehicles", params={"limit": 5}, headers=h).status_code == 200
    assert b.post("/auth/refresh", json={"refresh_token": refresh}).status_code == 200  # refresh works on the survivor
    assert b.post("/auth/refresh", json={"refresh_token": refresh}).status_code == 401  # and rotation state is shared


def test_redis_outage_degrades_the_api_instead_of_failing(ctx):
    from cvi.api.app import create_app

    c = TestClient(create_app())
    h = login(c, "fleet_manager@acme.demo")

    def boom(*a, **k):
        raise ConnectionError("redis down")

    ctx.state.r.get = boom
    ctx.state.allow = boom
    ctx.state.ping = lambda: False
    r = c.get("/api/v1/vehicles", params={"limit": 5}, headers=h)
    assert r.status_code == 200  # rate limiter fell back to the in-process bucket; reads come from PostgreSQL
    ready = c.get("/health/ready").json()
    assert ready["status"] == "degraded" and ready["checks"]["redis"] is False and ready["checks"]["postgres"] is True
    assert pump(ctx) >= 0
