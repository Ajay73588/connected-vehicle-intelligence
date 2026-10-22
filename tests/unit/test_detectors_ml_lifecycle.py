import io
import random
import time

import pyarrow.parquet as pq
import pytest
from helpers import VIN, event

from cvi.lifecycle.erasure import _scrub_parquet
from cvi.lifecycle.history import vehicle_hourly_history
from cvi.lifecycle.jobs import compact_day, expire_tiers, run_compaction
from cvi.ml.features import FEATURES
from cvi.ml.model import MaintenanceModel
from cvi.ml.synthetic import generate
from cvi.ml.train import baseline_scores, evaluate, train
from cvi.simulator.scenarios import SCENARIOS
from cvi.storage.object_store import LocalObjectStore, write_events
from cvi.stream.detectors import DetectorEngine, Geofence, VehicleState


def run(events, engine=None):
    eng = engine or DetectorEngine()
    st = VehicleState(events[0]["vin"], events[0]["tenant"])
    found: dict[str, list] = {}
    for e in events:
        al, _ = eng.update(st, e, True)
        st.last_ts = e["ts"]
        for a in al:
            found.setdefault(a.type, []).append(a)
    return found, st


def test_engine_failure_scenario_raises_overheat_dtc_and_oil_alerts_and_drops_health():
    evs = SCENARIOS["engine_failure"](int(time.time() * 1000), 1, random.Random(1))
    found, st = run(evs)
    assert {"engine_overheating", "repeated_dtc", "low_oil_pressure"} <= set(found)
    assert all(a.severity == "critical" for a in found["engine_overheating"])
    score, factors = st.health()
    assert score < 40 and {"overheating", "diagnostic_codes", "low_oil_pressure"} <= set(factors)


def test_dangerous_driver_scenario_and_risk_score():
    found, st = run(SCENARIOS["dangerous_driver"](int(time.time() * 1000), 1, random.Random(1)))
    assert "dangerous_driving" in found
    assert st.safety()["risk_score"] >= 85 and st.safety()["harsh_brake_30m"] > 5


def test_ev_battery_scenario():
    found, st = run(SCENARIOS["ev_battery"](int(time.time() * 1000), 1, random.Random(1)))
    assert {"battery_degradation", "battery_thermal"} <= set(found)
    assert any(a.severity == "critical" for a in found["battery_degradation"])  # SoH < 70 reached
    assert st.signals()["soh"] < 70


def test_idle_alert_reports_cost_from_configured_assumptions():
    evs = SCENARIOS["fleet_idling"](int(time.time() * 1000), 1, random.Random(1))
    first_vin = evs[0]["vin"]
    found, _ = run([e for e in evs if e["vin"] == first_vin], DetectorEngine(idle_fuel_lph=2.5, fuel_price=1.45))
    a = found["excessive_idling"][-1]
    assert a.details["fuel_litres"] == pytest.approx(a.details["idle_minutes"] / 60 * 2.5, abs=0.05)
    assert a.details["cost_usd"] == pytest.approx(a.details["fuel_litres"] * 1.45, abs=0.05)


def test_healthy_vehicle_raises_nothing():
    base = int(time.time() * 1000)
    found, st = run([event(i, base + i * 15_000) for i in range(1, 80)])
    assert found == {} and st.health()[0] == 100


def test_single_spike_is_not_an_alert():
    base = int(time.time() * 1000)
    evs = [event(i, base + i * 15_000) for i in range(1, 30)]
    evs[10] = event(11, base + 11 * 15_000, coolant_temp_c=130.0)
    found, _ = run(evs)
    assert "engine_overheating" not in found  # needs 3 samples in 2 minutes


def test_geofence_unexpected_location_and_restricted_zone():
    gf = {"acme-logistics": [Geofence("area", "operating", 41.88, -87.63, 5000), Geofence("airfield", "restricted", 41.90, -87.65, 1000)]}
    base = int(time.time() * 1000)
    far = [event(i, base + i * 10_000, lat=43.5, lon=-90.0) for i in range(1, 6)]
    found, _ = run(far, DetectorEngine(geofences=gf))
    assert "unexpected_location" in found
    inside = [event(i, base + i * 10_000, lat=41.90, lon=-87.65) for i in range(1, 6)]
    found2, _ = run(inside, DetectorEngine(geofences=gf))
    assert "restricted_zone" in found2 and "unexpected_location" not in found2


def test_movement_with_engine_off_is_suspicious():
    base = int(time.time() * 1000)
    found, _ = run([event(i, base + i * 5000, engine_on=False, speed_kmh=40.0) for i in range(1, 6)])
    assert found["suspicious_movement"][0].severity == "critical"


def test_out_of_order_events_update_windows_without_breaking_counters():
    eng, base = DetectorEngine(), 1_800_000_000_000
    st = VehicleState(VIN, "acme-logistics")
    eng.update(st, event(3, base + 3000), True)
    st.last_ts = base + 3000
    eng.update(st, event(2, base + 2000, harsh_brake=True), False)
    assert st.safety()["harsh_brake_30m"] == 1


# ---------------------------------------------------------------- machine learning
def test_ml_training_beats_baseline_on_held_out_data_and_is_reproducible(tmp_path):
    r1 = train(n=12_000, seed=3, out_dir=str(tmp_path / "a"))
    r2 = train(n=12_000, seed=3, out_dir=str(tmp_path / "b"))
    assert r1["model_test"] == r2["model_test"]  # deterministic
    assert r1["model_test"]["roc_auc"] > r1["baseline_rule_test"]["roc_auc"] + 0.1
    assert r1["model_test"]["pr_auc"] > r1["baseline_rule_test"]["pr_auc"]
    assert "SYNTHETIC" in r1["data"]["kind"]
    assert (tmp_path / "a" / "LATEST").read_text() == r1["version"]


def test_model_explains_a_risky_vehicle_with_sensible_factors(tmp_path):
    import joblib

    r = train(n=15_000, seed=4, out_dir=str(tmp_path))
    m = MaintenanceModel(joblib.load(tmp_path / r["version"] / "model.joblib"))
    X, y, wear = generate(2000, seed=9)
    risky = dict(zip(FEATURES, X[wear.argmax()], strict=True))
    healthy = dict(zip(FEATURES, X[wear.argmin()], strict=True))
    p_r, p_h = m.predict([risky, healthy])
    assert p_r > p_h
    assert m.explain(risky) and all(f["risk_contribution"] > 0 for f in m.explain(risky))


def test_evaluate_and_baseline_shapes():
    X, y, _ = generate(3000, seed=1)
    ev = evaluate(y, baseline_scores(X))
    assert 0.4 < ev["roc_auc"] < 0.9 and 0 <= ev["top10pct"]["precision"] <= 1


# ---------------------------------------------------------------- object storage / lifecycle / erasure
def mk_events(vin, n, ts0, tenant="acme-logistics"):
    return [event(i, ts0 + i * 1000, vin=vin, tenant=tenant) for i in range(n)]


def test_parquet_write_is_idempotent_and_partitioned(tmp_path):
    store = LocalObjectStore(str(tmp_path))
    evs = mk_events(VIN, 20, 1_800_000_000_000)
    k1 = write_events(store, evs)
    k2 = write_events(store, evs)  # replay of the same batch
    assert k1 == k2 and len(store.list("telemetry/")) == 1
    assert "tier=warm/tenant=acme-logistics/date=" in k1[0] and "/hour=" in k1[0]
    t = pq.read_table(io.BytesIO(store.get(k1[0])))
    assert t.num_rows == 20 and t["vin"].to_pylist() == [VIN] * 20


def test_compaction_merges_dedupes_and_removes_warm_files(tmp_path):
    store = LocalObjectStore(str(tmp_path))
    ts0 = 1_700_000_000_000  # Nov 2023: safely in the past
    write_events(store, mk_events(VIN, 10, ts0))
    write_events(store, mk_events(VIN, 15, ts0))  # overlapping replay with extra rows -> different file, duplicate ids
    assert len(store.list("telemetry/tier=warm/")) == 2
    done = run_compaction(store, older_than_hours=0)
    assert done and done[0]["rows"] == 15  # 10 duplicates dropped
    assert not store.list("telemetry/tier=warm/") and len(store.list("telemetry/tier=cold/")) == 1
    assert compact_day(store, "acme-logistics", done[0]["date"])["files_in"] == 0  # idempotent


def test_retention_deletes_only_expired_partitions(tmp_path):
    store = LocalObjectStore(str(tmp_path))
    write_events(store, mk_events(VIN, 3, 1_500_000_100_000))  # 2017 -> expired
    write_events(store, mk_events(VIN, 3, int(time.time() * 1000)))  # today -> kept
    removed = expire_tiers(store, warm_days=30, cold_days=730)
    assert removed["warm"] == 1 and len(store.list("telemetry/tier=warm/")) == 1


def test_erasure_scrubs_only_the_subject_from_parquet(tmp_path):
    store = LocalObjectStore(str(tmp_path))
    other = "5YJ" + "A" * 14
    now = int(time.time() * 1000)
    write_events(store, mk_events(VIN, 5, now) + mk_events(other, 5, now))
    rep = _scrub_parquet(store, "acme-logistics", VIN)
    assert rep["rows_removed"] == 5 and rep["files_rewritten"] == 1
    t = pq.read_table(io.BytesIO(store.get(store.list("telemetry/")[0])))
    assert set(t["vin"].to_pylist()) == {other}
    assert _scrub_parquet(store, "acme-logistics", VIN)["rows_removed"] == 0


def test_duckdb_history_over_parquet_is_replay_safe(tmp_path):
    store = LocalObjectStore(str(tmp_path))
    now = int(time.time() * 1000)
    evs = [event(i, now - 600_000 + i * 1000, harsh_brake=(i % 5 == 0)) for i in range(60)]
    write_events(store, evs)
    write_events(store, evs[:30])  # duplicate partial replay in another file
    pts = vehicle_hourly_history(store, "acme-logistics", VIN, hours=3)
    assert sum(p["samples"] for p in pts) == 60  # duplicates removed by event_id
    assert sum(p["harsh_brakes"] for p in pts) == 12
    assert vehicle_hourly_history(store, "acme-logistics", "5YJ" + "B" * 14, hours=3) == []
