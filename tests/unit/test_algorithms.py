import itertools
import random

import pytest

from cvi.algorithms.geo import (
    geohash_decode,
    geohash_encode,
    geohash_neighbors,
    haversine_km,
    in_circle,
    in_polygon,
)
from cvi.algorithms.graph import astar, dijkstra, grid_road_graph, knapsack_schedule, reconstruct
from cvi.algorithms.obd import (
    decode_dtc,
    encode_dtc,
    encode_mode1,
    parse_mode1_response,
    parse_mode3_response,
)
from cvi.algorithms.sketches import BloomFilter, CountMinSketch, SpaceSavingTopK, top_k_exact
from cvi.algorithms.vin import check_digit, is_valid_vin, make_vin
from cvi.algorithms.windowing import OnlineStats, SlidingWindow


# ---------------------------------------------------------------- sketches
def test_bloom_has_no_false_negatives_and_bounded_false_positives():
    bf = BloomFilter(capacity=20_000, error_rate=0.01)
    for i in range(20_000):
        bf.add(f"evt-{i}")
    assert all(f"evt-{i}" in bf for i in range(20_000))
    fp = sum(f"other-{i}" in bf for i in range(20_000)) / 20_000
    assert fp < 0.03  # configured 1%; allow slack for variance


def test_bloom_add_reports_prior_presence():
    bf = BloomFilter(100)
    assert bf.add("a") is False
    assert bf.add("a") is True


def test_count_min_never_undercounts_and_error_is_bounded():
    cms = CountMinSketch(eps=0.01, delta=0.01)
    truth = {}
    rnd = random.Random(1)
    for _ in range(20_000):
        k = f"dtc-{int(rnd.paretovariate(1.2)) % 200}"
        truth[k] = truth.get(k, 0) + 1
        cms.add(k)
    for k, v in truth.items():
        est = cms.estimate(k)
        assert est >= v
        assert est - v <= 0.01 * cms.total * 3  # eps*N with slack for the delta tail


def test_space_saving_finds_heavy_hitters():
    ss = SpaceSavingTopK(k=5)
    for _ in range(500):
        ss.add("hot-1")
    for _ in range(300):
        ss.add("hot-2")
    for i in range(400):
        ss.add(f"noise-{i}")
    top = [k for k, _ in ss.top()[:2]]
    assert top == ["hot-1", "hot-2"]


def test_top_k_exact():
    assert top_k_exact({"a": 1, "b": 5, "c": 3}, 2) == [("b", 5), ("c", 3)]


# ---------------------------------------------------------------- geo
def test_geohash_known_vector_and_roundtrip():
    assert geohash_encode(57.64911, 10.40744, 11) == "u4pruydqqvj"  # canonical example from the geohash spec
    lat, lon = geohash_decode(geohash_encode(41.8781, -87.6298, 9))
    assert abs(lat - 41.8781) < 1e-4 and abs(lon + 87.6298) < 1e-4


def test_geohash_neighbors_are_eight_distinct_adjacent_cells():
    gh = geohash_encode(47.6062, -122.3321, 5)
    nb = geohash_neighbors(gh)
    assert len(set(nb)) == 8 and gh not in nb
    for n in nb:
        assert haversine_km(*geohash_decode(gh), *geohash_decode(n)) < 15


def test_haversine_and_geofence():
    assert haversine_km(0, 0, 0, 1) == pytest.approx(111.19, rel=0.01)
    assert in_circle(41.8781, -87.6298, 41.8781, -87.63, 100)
    assert not in_circle(42.5, -87.6, 41.8781, -87.63, 1000)
    square = [(0, 0), (0, 10), (10, 10), (10, 0)]
    assert in_polygon(5, 5, square) and not in_polygon(15, 5, square)


# ---------------------------------------------------------------- vin / obd
def test_vin_validation():
    assert is_valid_vin("1HGCM82633A004352")  # known valid VIN
    assert not is_valid_vin("1HGCM82633A004353")  # bad check digit
    assert not is_valid_vin("1HGCM82633A00435")  # length
    assert not is_valid_vin("1HGCM82633AO04352")  # illegal letter O
    assert is_valid_vin("11111111111111111")  # weighted sum 89 -> 89 % 11 == 1 == the 9th character


@pytest.mark.parametrize("serial", [0, 1, 99, 123456, 99_999])
def test_generated_vins_are_valid_and_unique(serial):
    v = make_vin("1FT", serial)
    assert is_valid_vin(v) and v[8] == check_digit(v)


def test_generated_vins_unique_across_fleet():
    vins = {make_vin("1FT", i) for i in range(5000)}
    assert len(vins) == 5000


def test_obd_dtc_roundtrip_and_mode3():
    assert decode_dtc(0x01, 0x03) == "P0103"
    assert decode_dtc(0x43, 0x01) == "C0301"
    for code in ("P0301", "P0217", "U0100", "B1234", "C0300"):
        assert decode_dtc(*encode_dtc(code)) == code
    b1, b2 = encode_dtc("P0301")
    b3, b4 = encode_dtc("P0217")
    assert parse_mode3_response(f"43 02 {b1:02X} {b2:02X} {b3:02X} {b4:02X}") == ["P0301", "P0217"]


def test_obd_mode1_parse_known_values():
    out = parse_mode1_response("41 0C 1A F8 05 7B 0D 3C")
    assert out["rpm"] == pytest.approx(1726.0)  # (256*0x1A + 0xF8)/4
    assert out["coolant_temp_c"] == 83  # 0x7B - 40
    assert out["speed_kmh"] == 60
    with pytest.raises(ValueError):
        parse_mode1_response("42 0C 00 00")
    with pytest.raises(ValueError):
        parse_mode1_response("41 0C 1A")  # truncated
    assert parse_mode1_response(encode_mode1({"rpm": 2000, "speed_kmh": 80}))["rpm"] == pytest.approx(2000)


# ---------------------------------------------------------------- graph / dp
def test_dijkstra_matches_astar_on_grid():
    g, coords = grid_road_graph(12, 40.0, -88.0, 0.01)
    dist, prev = dijkstra(g, "0:0", "11:11")
    d_astar, path = astar(g, "0:0", "11:11", lambda n: haversine_km(*coords[n], *coords["11:11"]))
    assert d_astar == pytest.approx(dist["11:11"])
    assert path[0] == "0:0" and path[-1] == "11:11"
    assert len(path) == len(reconstruct(prev, "0:0", "11:11"))  # both produce a shortest path (same hop count on a uniform grid)


def test_astar_unreachable():
    d, p = astar({"a": [], "b": []}, "a", "b", lambda n: 0)
    assert d == float("inf") and p == []


def test_knapsack_is_optimal_vs_bruteforce():
    rnd = random.Random(3)
    items = [(f"v{i}", rnd.randint(1, 8), round(rnd.random(), 3)) for i in range(10)]
    cap = 15
    best = 0.0
    for r in range(len(items) + 1):
        for combo in itertools.combinations(items, r):
            if sum(c[1] for c in combo) <= cap:
                best = max(best, sum(c[2] for c in combo))
    value, chosen = knapsack_schedule(items, cap)
    assert value == pytest.approx(best)
    assert sum(c[1] for c in items if c[0] in chosen) <= cap
    assert sum(c[2] for c in items if c[0] in chosen) == pytest.approx(value)


# ---------------------------------------------------------------- windows
def test_sliding_window_handles_out_of_order_and_eviction():
    w = SlidingWindow(10)
    for ts in (1, 2, 3, 4, 5):
        w.add(ts, 1.0)
    w.add(2.5, 1.0)  # late but inside the window: inserted in order
    assert len(w) == 6 and w.total == 6
    w.add(30, 1.0)  # advances watermark -> everything older than 20 evicted
    assert len(w) == 1
    assert w.add(5, 1.0) is False  # older than the window now: rejected


def test_sliding_window_slope():
    w = SlidingWindow(1000)
    for t in range(50):
        w.add(t, 2.0 * t + 3)
    assert w.slope_per_s() == pytest.approx(2.0)


def test_online_stats_flags_outlier_only_after_warmup():
    s = OnlineStats(alpha=0.05)
    rnd = random.Random(2)
    zs = [s.update(90 + rnd.gauss(0, 1)) for _ in range(200)]
    assert max(abs(z) for z in zs[:20]) == 0  # no verdict before warm-up
    assert abs(s.update(130)) > 4
