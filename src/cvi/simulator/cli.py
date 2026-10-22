"""Simulator CLI.

  python simulator.py --mode demo                    # 1,000 vehicles, 300 ev/s, runs until Ctrl-C
  python simulator.py --vehicles 100000 --events-per-second 100000 --duration 300 --workers 8
  python simulator.py scenario engine_failure        # deterministic scripted scenario
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time

from cvi.common.config import get_settings
from cvi.simulator.chaos import ChaosInjector
from cvi.simulator.engine import ChaosConfig, FleetEngine
from cvi.simulator.scenarios import DESCRIPTIONS, RESERVED, run_scenario
from cvi.simulator.sinks import make_sink

MODES = {  # vehicles, events/s, workers
    "demo": (1_000, 300, 1),
    "small": (10_000, 2_000, 1),
    "load": (100_000, 20_000, 2),
    "100k": (100_000, 100_000, max(1, (os.cpu_count() or 4) - 1)),
}


def run_worker(shard: int, shards: int, vehicles: int, eps: float, duration: float, sink_name: str, seed: int,
               chaos: ChaosConfig, report: bool, burst: tuple[float, float, float], q=None) -> dict:
    engine = FleetEngine(vehicles, seed, shard, shards, reserved=RESERVED)
    inj = ChaosInjector(chaos, seed + shard, get_settings().allowed_lateness_s)
    sink = make_sink(sink_name)
    start = time.monotonic()
    slice_s, sent, generated = 0.05, 0, 0
    next_report = start + 1
    burst_every, burst_len, burst_factor = burst
    while duration <= 0 or time.monotonic() - start < duration:
        t = time.monotonic() - start
        rate = eps * (burst_factor if burst_every and (t % burst_every) < burst_len else 1.0)
        k = max(1, int(rate * slice_s))
        events = engine.step(k, int(time.time() * 1000))
        generated += len(events)
        wire = inj.apply(events)
        sink.send(wire)
        sent += len(wire)
        if report and time.monotonic() >= next_report:
            next_report += 1
            print(f"[sim shard {shard}] t={t:5.0f}s generated={generated:>9,} sent={sent:>9,} "
                  f"avg={sent / max(t, 1e-9):>9,.0f}/s chaos={inj.stats}", flush=True)
        lag = start + (generated / eps if eps else 0) - time.monotonic()  # pace to the target rate
        if lag > 0:
            time.sleep(min(lag, 0.5))
    sink.close()
    return {"shard": shard, "generated": generated, "sent": sent, "elapsed": time.monotonic() - start, **inj.stats}


def _worker_entry(args):
    return run_worker(*args)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Connected-vehicle telemetry simulator")
    p.add_argument("command", nargs="?", choices=["run", "scenario", "scenarios"], default="run")
    p.add_argument("name", nargs="?", help="scenario name")
    p.add_argument("--mode", choices=list(MODES))
    p.add_argument("--vehicles", type=int)
    p.add_argument("--events-per-second", type=float)
    p.add_argument("--duration", type=float, default=0, help="seconds, 0 = until interrupted")
    p.add_argument("--workers", type=int)
    p.add_argument("--sink", default="kafka", choices=["kafka", "http", "mqtt", "stdout", "null"])
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--no-chaos", action="store_true")
    p.add_argument("--burst", default="0:0:1", help="every:length:factor in seconds, e.g. 60:10:3 (3x burst for 10 s each minute)")
    a = p.parse_args(argv)

    if a.command == "scenarios":
        for k, v in DESCRIPTIONS.items():
            print(f"{k:22} {v}")
        return
    if a.command == "scenario":
        sink = make_sink(a.sink)
        n = run_scenario(a.name or "engine_failure", sink)
        sink.close()
        print(f"scenario {a.name}: sent {n} events")
        return

    vehicles, eps, workers = MODES[a.mode or "demo"]
    vehicles = a.vehicles or vehicles
    eps = a.events_per_second or eps
    workers = a.workers or workers
    chaos = ChaosConfig(0, 0, 0, 0) if a.no_chaos else ChaosConfig()
    be, bl, bf = (float(x) for x in a.burst.split(":"))
    print(f"simulator: {vehicles:,} vehicles, {eps:,.0f} events/s target, {workers} worker(s), sink={a.sink}, "
          f"duration={'∞' if not a.duration else a.duration}s", flush=True)
    jobs = [(w, workers, vehicles, eps / workers, a.duration, a.sink, a.seed, chaos, w == 0, (be, bl, bf)) for w in range(workers)]
    if workers == 1:
        results = [run_worker(*jobs[0])]
    else:
        with mp.Pool(workers) as pool:
            results = pool.map(_worker_entry, jobs)
    tot = sum(r["sent"] for r in results)
    el = max(r["elapsed"] for r in results)
    print(f"done: {tot:,} messages in {el:.1f}s = {tot / el:,.0f} msg/s")


if __name__ == "__main__":
    main()
