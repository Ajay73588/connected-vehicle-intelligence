#!/usr/bin/env python
"""API latency benchmark against a RUNNING API (compose or `python -m cvi.local`): N concurrent clients with keep-alive
connections issue realistic reads; reports throughput and p50/p95/p99 per endpoint (client-side wall time).

  python benchmarks/bench_api.py --base http://localhost:8000 --clients 16 --seconds 20
Note: the Python load generator and the API share this machine, so numbers are conservative for the API alone."""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--clients", type=int, default=16)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--email", default="fleet_manager@acme.demo")
    a = ap.parse_args()
    c0 = httpx.Client(base_url=a.base, timeout=30)
    tok = c0.post("/auth/login", json={"email": a.email, "password": "Demo@12345"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    first = c0.get("/api/v1/vehicles", params={"limit": 50}, headers=h).json()
    ids = [v["id"] for v in first["items"]]
    endpoints = {
        "GET /vehicles?limit=50": lambda c, i: c.get("/api/v1/vehicles", params={"limit": 50}, headers=h),
        "GET /vehicles (keyset page 2)": lambda c, i: c.get("/api/v1/vehicles", params={"limit": 50, "cursor": first["next_cursor"]}, headers=h),
        "GET /vehicles/{id}": lambda c, i: c.get(f"/api/v1/vehicles/{ids[i % len(ids)]}", headers=h),
        "GET /alerts?status=open": lambda c, i: c.get("/api/v1/alerts", params={"status": "open", "limit": 50}, headers=h),
        "GET /analytics/summary": lambda c, i: c.get("/api/v1/analytics/summary", headers=h),
        "GET /analytics/maintenance": lambda c, i: c.get("/api/v1/analytics/maintenance", headers=h),
        "GET /map/vehicles": lambda c, i: c.get("/api/v1/map/vehicles", params={"lat_min": 41.4, "lat_max": 42.4, "lon_min": -88.2, "lon_max": -87.1}, headers=h),
    }
    result = {"clients": a.clients, "seconds": a.seconds, "endpoints": {}}
    for name, fn in endpoints.items():
        lats, errs, codes = [], 0, {}
        stop = time.time() + a.seconds / len(endpoints) * 1.0

        def run(k: int, fn=fn, stop=stop, lats=lats, codes=codes):  # bind per-endpoint state explicitly
            nonlocal errs
            with httpx.Client(base_url=a.base, timeout=30) as c:
                i = k
                while time.time() < stop:
                    t0 = time.perf_counter()
                    r = fn(c, i)
                    if r.status_code == 200:  # latency statistics only for successful requests (429/5xx are reported in `codes`)
                        lats.append((time.perf_counter() - t0) * 1000)
                    codes[r.status_code] = codes.get(r.status_code, 0) + 1
                    if r.status_code >= 500:
                        errs += 1
                    i += a.clients

        ts = [threading.Thread(target=run, args=(k,)) for k in range(a.clients)]
        t0 = time.time()
        [t.start() for t in ts]
        [t.join() for t in ts]
        el = time.time() - t0
        result["endpoints"][name] = {"requests": sum(codes.values()), "ok_requests": len(lats), "rps": round(sum(codes.values()) / el, 1), "p50_ms": round(pct(lats, .5), 1),
                                     "p95_ms": round(pct(lats, .95), 1), "p99_ms": round(pct(lats, .99), 1),
                                     "mean_ms": round(statistics.mean(lats), 1), "status_codes": codes, "server_errors": errs}
        e = result["endpoints"][name]
        print(f"{name:32} {e['rps']:>8} rps  p50 {e['p50_ms']:>7} ms  p95 {e['p95_ms']:>7} ms  p99 {e['p99_ms']:>7} ms  codes {codes}", flush=True)
    out = ROOT / "benchmarks" / "results" / "api.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
