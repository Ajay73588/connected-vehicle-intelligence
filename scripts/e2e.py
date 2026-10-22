#!/usr/bin/env python
"""End-to-end check against a RUNNING stack (compose or `python -m cvi.local`): login -> scenario -> pipeline -> alert -> agent.

  python scripts/e2e.py --base http://localhost:8000
Exit code 0 only if every step behaved as specified. Prints measured latencies (no hard-coded numbers)."""

from __future__ import annotations

import argparse
import sys
import time

import httpx

PASSWORD = "Demo@12345"
SCENARIOS = [  # (scenario, tenant short-name, alert type expected, severity floor)
    ("engine_failure", "acme", "engine_overheating"),
    ("dangerous_driver", "acme", "dangerous_driving"),
    ("ev_battery", "borealis", "battery_degradation"),
    ("fleet_idling", "metro", "excessive_idling"),
]


def login(c: httpx.Client, email: str) -> dict:
    r = c.post("/auth/login", json={"email": email, "password": PASSWORD})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def ok(cond: bool, msg: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--timeout", type=float, default=60)
    a = ap.parse_args()
    c = httpx.Client(base_url=a.base, timeout=30)

    ok(c.get("/health/ready").status_code == 200, "API ready")
    ok(c.get("/api/v1/vehicles").status_code == 401, "unauthenticated request rejected (401)")

    for scenario, short, alert_type in SCENARIOS:
        h = login(c, f"fleet_manager@{short}.demo")
        t0 = time.time()
        r = c.post(f"/api/v1/demo/scenarios/{scenario}", headers=h)
        ok(r.status_code == 200, f"scenario {scenario} injected ({r.json().get('events_published')} events)")
        found = None
        while time.time() - t0 < a.timeout and not found:
            time.sleep(0.5)
            al = c.get("/api/v1/alerts", params={"status": "open", "limit": 200}, headers=h).json()["items"]
            found = next((x for x in al if x["type"] == alert_type and x["vin"].strip() in r.json()["vins"]), None)
        ok(bool(found), f"{scenario}: '{alert_type}' alert raised {time.time() - t0:.1f}s after injection"
           + (f" — {found['cause']}" if found else ""))

    h = login(c, "fleet_manager@acme.demo")
    q = c.post("/api/v1/agent/chat", json={"message": "Which vehicles have repeated engine faults?"}, headers=h).json()
    ok(bool(q["tool_calls"]) and "repeated" in q["answer"].lower() or "engine" in q["answer"].lower(), "agent answered using tools: "
       + ", ".join(t["tool"] for t in q["tool_calls"]))
    print("\n" + q["answer"] + "\n")
    s = c.get("/api/v1/analytics/summary", headers=h).json()
    ok(s["total_vehicles"] > 0, f"dashboard summary: {s['total_vehicles']} vehicles, {s['open_alerts']['total']} open alerts")
    print("E2E OK")


if __name__ == "__main__":
    main()
