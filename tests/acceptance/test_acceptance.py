"""BDD acceptance tests (pytest-bdd). They drive the real HTTP API, the real validator/stream-processor code and the real
PostgreSQL schema; only the transport (broker/Redis/Mongo) is in-memory."""

import random
import time

import orjson
import pytest
from conftest import login
from helpers import pump
from pytest_bdd import given, parsers, scenarios, then, when

from cvi.messaging.base import TOPIC_RAW
from cvi.simulator.scenarios import SCENARIO_VEHICLES, SCENARIOS
from cvi.synthetic.identity import vehicle_identity

pytestmark = pytest.mark.integration
scenarios("features/fleet_intelligence.feature")


@pytest.fixture()
def world(client, ctx):
    return {"client": client, "ctx": ctx, "h": None, "vin": None, "answer": None, "last_alert": None}


@given(parsers.parse('I am signed in as "{email}"'))
def signed_in(world, email):
    world["h"] = login(world["client"], email)


@when(parsers.parse('the "{name}" scenario is injected and the pipeline has processed it'))
def inject(world, name):
    r = world["client"].post(f"/api/v1/demo/scenarios/{name}", headers=world["h"])
    assert r.status_code == 200, r.text
    world["vins"] = r.json()["vins"]
    world["vin"] = world["vins"][0]
    pump(world["ctx"])


def open_alerts(world, **params):
    items = world["client"].get("/api/v1/alerts", params={"status": "open", "limit": 200, **params}, headers=world["h"]).json()["items"]
    return items


@then(parsers.parse('an open "{severity}" alert of type "{atype}" exists for the scenario vehicle'))
def alert_exists(world, severity, atype):
    hits = [a for a in open_alerts(world) if a["type"] == atype and a["vin"].strip() == world["vin"]]
    assert hits, f"no {atype} alert for {world['vin']}"
    assert any(a["severity"] == severity for a in hits)
    world["last_alert"] = hits[0]


@then(parsers.parse("the scenario vehicle health score is below {n:d}"))
def health_below(world, n):
    vid = next(v["id"] for v in world["client"].get("/api/v1/vehicles", params={"limit": 200, "q": world["vin"][:10]}, headers=world["h"]).json()["items"]
               if v["vin"].strip() == world["vin"])
    assert world["client"].get(f"/api/v1/vehicles/{vid}", headers=world["h"]).json()["vehicle"]["health_score"] < n


@when(parsers.parse('I ask the copilot "{question}"'))
def ask(world, question):
    if "the vehicle" in question:
        question = question.replace("the vehicle", world["vin"])
    r = world["client"].post("/api/v1/agent/chat", json={"message": question}, headers=world["h"])
    assert r.status_code == 200
    world["answer"] = r.json()


@then(parsers.parse('the answer mentions "{text}"'))
def mentions(world, text):
    assert text.lower() in world["answer"]["answer"].lower(), world["answer"]["answer"]


@then(parsers.parse('the copilot cites the tools "{a}" and "{b}"'))
def cites_two(world, a, b):
    tools = {t["tool"] for t in world["answer"]["tool_calls"]}
    assert {a, b} <= tools


@then(parsers.parse('the copilot cites the tools "{a}"'))
def cites_one(world, a):
    assert a in {t["tool"] for t in world["answer"]["tool_calls"]}


@then("the copilot recommends at least one action")
def has_action(world):
    assert world["answer"]["recommended_actions"]


@then("the answer lists the scenario vehicle")
def lists_vehicle(world):
    assert world["vin"] in world["answer"]["answer"], world["answer"]["answer"]


@then(parsers.parse('at least {n:d} vehicles have an open "{atype}" alert'))
def many_vehicles(world, n, atype):
    vins = {a["vin"].strip() for a in open_alerts(world) if a["type"] == atype}
    assert len(vins & set(world["vins"])) >= n


@when(parsers.parse('every event of the "{name}" scenario is delivered three times in random order'))
def triple(world, name):
    now = int(time.time() * 1000)
    evs = SCENARIOS[name](now, 777, random.Random(5))
    wire = evs * 3
    random.Random(1).shuffle(wire)
    for e in wire:
        world["ctx"].broker.produce(TOPIC_RAW, e["vin"], orjson.dumps(e), {"ingest_ts": str(now)})
    pump(world["ctx"])
    world["events"], world["now"] = evs, now


@then("each distinct event is counted exactly once")
def counted_once(world):
    vin = world["events"][0]["vin"]
    n = world["ctx"].state.vehicle_day(vin, time.strftime("%Y%m%d", time.gmtime(world["now"] / 1000)))["n"]
    assert n == len(world["events"])


@given(parsers.parse('I note a vehicle belonging to tenant "{short}"'))
def note_other(world, short):
    other = login(world["client"], f"admin@{short}.demo")
    v = world["client"].get("/api/v1/vehicles", params={"limit": 1}, headers=other).json()["items"][0]
    world["other"] = v


@then("fetching that vehicle returns 404")
def other_404(world):
    assert world["client"].get(f"/api/v1/vehicles/{world['other']['id']}", headers=world["h"]).status_code == 404


@then("the copilot refuses to discuss it")
def refuses(world):
    r = world["client"].post("/api/v1/agent/chat", json={"message": f"Tell me about {world['other']['vin'].strip()}"}, headers=world["h"]).json()
    assert "No vehicle" in r["answer"] and "health score" not in r["answer"].lower()


@when("I ask the copilot to create an alert for one of my vehicles")
def ask_create(world):
    vin = world["client"].get("/api/v1/vehicles", params={"limit": 1}, headers=world["h"]).json()["items"][0]["vin"].strip()
    world["vin"] = vin
    world["before"] = len([a for a in open_alerts(world) if a["type"] == "manual"])
    world["answer"] = world["client"].post("/api/v1/agent/chat", json={"message": f"Create an alert for {vin}: wiper fault"}, headers=world["h"]).json()


@then("no alert is created until I confirm")
def not_yet(world):
    assert world["answer"]["pending_confirmation"]
    assert len([a for a in open_alerts(world) if a["type"] == "manual"]) == world["before"]


@then("confirming creates exactly one alert")
def confirm(world):
    r = world["client"].post("/api/v1/agent/confirm", json={"token": world["answer"]["pending_confirmation"]["token"]}, headers=world["h"])
    assert r.status_code == 200 and r.json()["executed"]
    assert len([a for a in open_alerts(world) if a["type"] == "manual"]) == world["before"] + 1


_ = (SCENARIO_VEHICLES, vehicle_identity)
