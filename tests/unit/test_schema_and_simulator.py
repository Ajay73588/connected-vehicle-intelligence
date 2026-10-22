import json
import time
from pathlib import Path

import jsonschema
import orjson
import pytest

from cvi.algorithms.obd import encode_mode1
from cvi.ingestion.validator import validate_payload
from cvi.schemas.telemetry import telemetry_json_schema
from cvi.simulator.chaos import ChaosInjector
from cvi.simulator.engine import ChaosConfig, FleetEngine
from cvi.simulator.scenarios import RESERVED, SCENARIO_VEHICLES, SCENARIOS, run_scenario
from cvi.simulator.sinks import MemorySink
from cvi.synthetic.identity import vehicle_identity

ROOT = Path(__file__).resolve().parents[2]


def good(**kw):
    e = {"schema_version": 1, "event_id": "1FT-event-1", "tenant": "acme-logistics", "vin": vehicle_identity(0)["vin"], "seq": 1,
         "ts": int(time.time() * 1000), "lat": 41.8, "lon": -87.6, "speed_kmh": 40.0, "odometer_km": 100.0, "fuel_type": "diesel"}
    e.update(kw)
    return e


def check(payload):
    return validate_payload(orjson.dumps(payload))


def test_valid_event_passes_and_invalid_ones_are_classified():
    assert check(good())[0] is not None
    assert check(good(speed_kmh=-5))[1] == "schema_violation"
    assert check(good(vin="1HGCM82633A004353"))[1] == "invalid_vin"
    assert check(good(lat=95))[1] == "schema_violation"
    assert check(good(ts=int(time.time() * 1000) + 3_600_000))[1] == "timestamp_in_future"
    assert check(good(dtc=["XYZ"]))[1] == "schema_violation"
    assert check(good(surprise="field"))[1] == "schema_violation"  # extra="forbid": contract is strict
    assert validate_payload(b"{truncated")[1] == "malformed"
    assert validate_payload(b"[1,2]")[1] == "malformed"


def test_obd_frame_is_decoded_into_signals():
    ev, reason, _ = check(good(obd_raw=encode_mode1({"rpm": 1800, "coolant_temp_c": 92, "speed_kmh": 40})))
    assert reason == "" and ev["rpm"] == pytest.approx(1800) and ev["coolant_temp_c"] == 92
    assert check(good(obd_raw="41 FF 00"))[1] == "obd_parse_error"


def test_json_schema_contract_file_is_current_and_enforced():
    published = json.loads((ROOT / "schemas" / "telemetry.v1.schema.json").read_text())
    assert published == telemetry_json_schema(), "run `python scripts/export_schema.py` after changing the event model"
    jsonschema.validate(good(), published)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(good(speed_kmh=999), published)


def test_clean_simulated_events_are_100_percent_valid_for_all_fuel_types():
    eng = FleetEngine(400, seed=1, reserved=RESERVED)
    now = int(time.time() * 1000)
    fuels = set()
    for _ in range(4):
        for e in eng.step(300, now):
            ev, reason, detail = validate_payload(orjson.dumps(e))
            assert ev is not None, (reason, detail, e)
            fuels.add(e["fuel_type"])
    assert fuels == {"diesel", "petrol", "ev", "hybrid"}


def test_simulator_is_deterministic_and_vehicle_state_persists():
    a, b = FleetEngine(200, seed=5), FleetEngine(200, seed=5)
    ea, eb = a.step(100, 1_800_000_000_000), b.step(100, 1_800_000_000_000)
    assert ea == eb
    seq_before = {e["vin"]: e["seq"] for e in ea}
    again = a.step(200, 1_800_000_010_000)
    for e in again:
        if e["vin"] in seq_before:
            assert e["seq"] == seq_before[e["vin"]] + 1  # per-vehicle monotonic sequence
            assert e["event_id"] == f"{e['vin']}-{e['seq']}"


def test_chaos_injects_every_fault_class():
    eng = FleetEngine(2000, seed=2, reserved=RESERVED)
    inj = ChaosInjector(ChaosConfig(duplicate_rate=0.05, out_of_order_rate=0.05, late_rate=0.02, malformed_rate=0.05, outage_vehicle_share=0.05), seed=1)
    wire = []
    for _ in range(40):
        wire += inj.apply(eng.step(500, int(time.time() * 1000)))
    assert all(v > 0 for v in inj.stats.values()), inj.stats
    reasons = {validate_payload(v)[1] for _, v in wire} - {""}
    assert {"malformed", "invalid_vin", "schema_violation", "timestamp_in_future"} <= reasons
    ids = [orjson.loads(v).get("event_id") for _, v in wire if validate_payload(v)[0]]
    assert len(ids) > len(set(ids))  # real duplicates are present on the wire


def test_scenarios_use_valid_reserved_vehicles_and_are_excluded_from_the_fleet():
    fleet_vins = set(FleetEngine(300, reserved=RESERVED).vins)
    assert RESERVED and not ({vehicle_identity(i)["vin"] for i in RESERVED} & fleet_vins)
    for name, fn in SCENARIOS.items():
        for e in fn(int(time.time() * 1000), 1, __import__("random").Random(0)):
            assert validate_payload(orjson.dumps(e))[0] is not None, name
    sink = MemorySink()
    assert run_scenario("engine_failure", sink) == len(sink.msgs) == 40
    with pytest.raises(KeyError):
        run_scenario("nope", sink)
    assert set(SCENARIO_VEHICLES) == set(SCENARIOS)
