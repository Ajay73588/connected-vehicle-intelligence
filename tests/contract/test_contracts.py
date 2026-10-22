"""Contract checks at service boundaries: every message/response a service produces is validated against the published
JSON Schema in /schemas, and the REST surface is pinned by a snapshot."""

import json
import tempfile
import time
from pathlib import Path

import fakeredis
import jsonschema
import orjson
import pytest
from conftest import login  # noqa: F401
from helpers import MemorySink, event, msgs

from cvi.ingestion.validator import Validator
from cvi.messaging.base import TOPIC_ALERTS, TOPIC_DLQ, TOPIC_RAW
from cvi.messaging.memory import MemoryBroker
from cvi.storage.mongo_store import MemoryRawStore
from cvi.storage.object_store import LocalObjectStore
from cvi.storage.redis_store import StateStore
from cvi.stream.processor import Processor

ROOT = Path(__file__).resolve().parents[2]


def schema(name):
    return json.loads((ROOT / "schemas" / name).read_text())


def test_alert_topic_messages_conform_to_alert_schema():
    broker = MemoryBroker(2)
    broker.ensure_topics()
    proc = Processor(broker, StateStore(fakeredis.FakeRedis(decode_responses=True)), MemoryRawStore(), LocalObjectStore(tempfile.mkdtemp()), MemorySink())
    base = int(time.time() * 1000)
    proc.process_batch(msgs([event(i, base + i * 15_000, coolant_temp_c=125.0, dtc=["P0301"]) for i in range(1, 12)]))
    out = broker.messages(TOPIC_ALERTS)
    assert out
    for m in out:
        jsonschema.validate(orjson.loads(m.value), schema("alert.v1.schema.json"))


def test_dlq_messages_conform_to_dlq_schema():
    b = MemoryBroker(2)
    b.ensure_topics()
    for payload in (b"{broken", orjson.dumps(event(1, speed_kmh=-1)), orjson.dumps(event(1, vin="1FTAAAAAAAAAAAAAA")),
                    orjson.dumps(event(2, ts=int(time.time() * 1000) + 7_200_000))):
        b.produce(TOPIC_RAW, "k", payload, {})
    v = Validator(b)
    v.handle(v.consumer.poll_batch(100, 0))
    dlq = b.messages(TOPIC_DLQ)
    assert len(dlq) == 4
    for m in dlq:
        jsonschema.validate(orjson.loads(m.value), schema("dlq.v1.schema.json"))


@pytest.mark.integration
def test_rest_responses_conform_to_published_schemas(client, acme_manager):
    a = client.get("/api/v1/alerts", params={"limit": 20}, headers=acme_manager).json()["items"]
    assert a
    for item in a:
        jsonschema.validate(item, schema("api_alert_item.v1.schema.json"))
    jsonschema.validate(client.get("/api/v1/analytics/summary", headers=acme_manager).json(), schema("api_summary.v1.schema.json"))
    for q in ("Which vehicles are at highest maintenance risk?", "hello there"):
        jsonschema.validate(client.post("/api/v1/agent/chat", json={"message": q}, headers=acme_manager).json(), schema("agent_response.v1.schema.json"))


@pytest.mark.integration
def test_rest_surface_matches_the_pinned_openapi_snapshot(client):
    spec = client.get("/openapi.json").json()
    surface = sorted(f"{m.upper()} {p}" for p, ops in spec["paths"].items() for m in ops)
    snap = [s for s in (ROOT / "tests" / "contract" / "openapi_surface.txt").read_text().split("\n") if s]
    assert surface == snap, "API surface changed: review, then run `python scripts/snapshot_api.py`"


@pytest.mark.integration
def test_every_protected_route_requires_a_token(client):
    spec = client.get("/openapi.json").json()
    public = {"/auth/login", "/auth/refresh", "/health/live", "/health/ready", "/metrics"}
    for path, ops in spec["paths"].items():
        if path in public:
            continue
        for method in ops:
            url = path.replace("{vehicle_id}", "1").replace("{alert_id}", "1").replace("{name}", "x")
            r = client.request(method.upper(), url)
            assert r.status_code == 401, (method, path, r.status_code)  # never served (or even validated) without a token
