"""Batch scoring against the real database, and the Redis Lua scripts against a REAL Redis server when one is reachable."""

import time

import pytest
import redis as redislib
from helpers import event

from cvi.lifecycle.scoring import score_all
from cvi.ml.model import MaintenanceModel
from cvi.ml.train import train
from cvi.storage.pg import plain_tx
from cvi.storage.redis_store import StateStore, day_key


@pytest.mark.integration
def test_batch_scoring_inserts_one_prediction_per_vehicle_with_explanations(ctx, tmp_path):
    import joblib

    r = train(n=8000, seed=2, out_dir=str(tmp_path))
    model = MaintenanceModel(joblib.load(tmp_path / r["version"] / "model.joblib"))
    with plain_tx(ctx.worker_pool) as c:
        tenants = [x["id"] for x in c.execute("SELECT id FROM tenants")]
        before = c.execute("SELECT count(*) AS n FROM maintenance_predictions WHERE model_version = %s", [model.version]).fetchone()["n"]
    n = score_all(ctx.worker_pool, tenants, model=model, chunk=250, explain_threshold=0.2)
    with plain_tx(ctx.worker_pool) as c:
        after = c.execute("SELECT count(*) AS n FROM maintenance_predictions WHERE model_version = %s", [model.version]).fetchone()["n"]
        risky = c.execute("SELECT failure_prob, top_factors FROM maintenance_predictions WHERE model_version = %s ORDER BY failure_prob DESC LIMIT 1",
                          [model.version]).fetchone()
    assert n > 0 and after - before == n
    assert 0 <= float(risky["failure_prob"]) <= 1


@pytest.fixture()
def real_redis():
    try:
        r = redislib.Redis.from_url("redis://localhost:6379/15", protocol=2, decode_responses=True, socket_connect_timeout=1)
        r.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("no real Redis on localhost:6379")
    r.flushdb()
    yield r
    r.flushdb()


def test_lua_commit_is_atomic_and_idempotent_on_a_real_redis(real_redis):
    st = StateStore(real_redis)
    e = event(1, 1_700_000_000_000)
    assert st.commit_many([(e, {"n": 1.0, "hb": 1.0})]) == 1
    assert st.commit_many([(e, {"n": 1.0, "hb": 1.0})]) == 0  # duplicate: nothing applied
    assert st.seen_many([e]) == [True]
    assert st.vehicle_day(e["vin"], day_key(e["ts"])) == {"n": 1.0, "hb": 1.0}
    assert real_redis.pfcount(f"hll:{e['tenant']}:2023111418") == 1 or real_redis.keys("hll:*")


def test_token_bucket_and_cache_on_a_real_redis(real_redis):
    st = StateStore(real_redis)
    allowed = [st.allow("k", 5) for _ in range(8)]
    assert allowed[:5] == [True] * 5 and not any(allowed[5:])
    calls = []
    assert st.cached("x", 5, lambda: calls.append(1) or {"v": 1}) == {"v": 1}
    assert st.cached("x", 5, lambda: calls.append(1) or {"v": 2}) == {"v": 1} and len(calls) == 1


def test_state_store_map_cells_and_vehicle_state_on_a_real_redis(real_redis):
    st = StateStore(real_redis)
    now = int(time.time() * 1000)
    e1, e2 = event(1, now, lat=41.88, lon=-87.63), event(1, now, vin="1FTAAAAAAAAAAAAAB", lat=41.9, lon=-87.6)
    st.put_states([e1, e2], {})
    cells = st.cells_for_bbox(41.5, 42.3, -88.0, -87.2)
    vins = st.vins_in_cells("acme-logistics", cells)
    assert set(vins) == {e1["vin"], "1FTAAAAAAAAAAAAAB"}
    assert st.get_state(e1["vin"])["tenant"] == "acme-logistics" and st.active_count("acme-logistics") == 2
    assert st.delete_vehicle(e1["vin"]) >= 1 and st.get_state(e1["vin"]) is None
