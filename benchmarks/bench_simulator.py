#!/usr/bin/env python
"""Simulator generation throughput (no sink): how many events/s one process and N processes can produce for 100K vehicles."""

import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def worker(args):
    shard, shards, vehicles, seconds = args
    from cvi.simulator.chaos import ChaosInjector
    from cvi.simulator.engine import ChaosConfig, FleetEngine
    from cvi.simulator.scenarios import RESERVED

    eng = FleetEngine(vehicles, shard=shard, shards=shards, reserved=RESERVED)
    inj = ChaosInjector(ChaosConfig())
    n, t0 = 0, time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        n += len(inj.apply(eng.step(2500, int(time.time() * 1000))))
    return n, time.perf_counter() - t0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicles", type=int, default=100_000)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--workers", type=int, nargs="*", default=[1, 2, 4])
    a = ap.parse_args()
    out = {"vehicles": a.vehicles, "logical_cores": os.cpu_count(), "runs": []}
    for w in a.workers:
        with mp.Pool(w) as pool:
            res = pool.map(worker, [(i, w, a.vehicles, a.seconds) for i in range(w)])
        total = sum(n for n, _ in res)
        el = max(t for _, t in res)
        out["runs"].append({"workers": w, "messages_per_s": int(total / el)})
        print(f"{w} worker(s): {int(total / el):,} msg/s", flush=True)
    p = ROOT / "benchmarks" / "results" / "simulator.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2))
