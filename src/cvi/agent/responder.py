"""Deterministic answer composition. Every figure printed comes from a tool result (nothing is estimated here except the
documented arithmetic in the tools). Returns (markdown answer, recommended actions)."""

from __future__ import annotations

from cvi.agent.planner import Plan


def _fmt_factors(factors: list[dict]) -> str:
    return "; ".join(f"{f['label']} ({f['value']})" for f in factors[:3]) or "no single dominant factor"


def compose(plan: Plan, R: dict[str, dict]) -> tuple[str, list[str]]:  # noqa: N803
    i = plan.intent
    actions: list[str] = []
    summ = R.get("get_fleet_summary", {}).get("summary")

    if i == "maintenance_risk":
        preds = R["get_maintenance_predictions"]["predictions"]
        if not preds:
            return "No vehicle currently exceeds the 30% predicted-failure threshold.", []
        lines = [f"**{len(preds)} vehicles have the highest 14-day failure risk** (model `{R['get_maintenance_predictions']['model_version']}`, trained on synthetic data):"]
        for k, p in enumerate(preds, 1):
            lines.append(f"{k}. `{p['vin']}` {p['make']} {p['model']} — {round(float(p['failure_prob']) * 100)}% — drivers: {_fmt_factors(p['top_factors'])}")
            if k <= 3:
                actions.append(f"Inspect {p['vin']} ({round(float(p['failure_prob']) * 100)}% failure risk) within 48 hours.")
        if summ:
            lines.append(f"\nFleet-wide, {summ['maintenance_risk']['vehicles_over_50pct']} vehicles are above 50% risk.")
        return "\n".join(lines), actions

    if i == "utilization":
        u = R["get_utilization_analysis"]
        daily = u["daily"]
        s = R["get_fleet_summary"]["summary"]
        crit = R["get_recent_alerts"]["count"]
        if len(daily) < 2:
            return f"Only {len(daily)} day(s) of utilization history exist, so no trend can be computed. Today: {s['utilization']['pct']}% of vehicles moved.", []
        prev = daily[:-1]
        avg_prev = sum(d["active_vehicles"] for d in prev) / len(prev)
        today = daily[-1]
        delta = today["active_vehicles"] - avg_prev
        verdict = "down" if delta < 0 else "up"
        lines = [f"Active vehicles today: **{today['active_vehicles']}** vs a {len(prev)}-day average of **{round(avg_prev)}** — {verdict} {abs(round(delta))}.",
                 "Contributing factors from the data:",
                 f"- {u['in_maintenance']} vehicles are currently in maintenance status.",
                 f"- {u['vehicles_with_critical_alerts']} vehicles have open critical alerts ({crit} most recent shown).",
                 f"- Idling today: {s['idling']['hours']} h ({s['idling']['est_cost_usd']} USD estimated cost)."]
        if delta < 0:
            actions += ["Clear critical alerts on grounded vehicles to bring them back into service.", "Review idle-heavy vehicles for scheduling inefficiency."]
        return "\n".join(lines), actions

    if i == "battery":
        if "get_vehicle_health" in R:
            return _vehicle(plan, R)
        al = R["get_recent_alerts"]["alerts"]
        pr = R["get_maintenance_predictions"]["predictions"]
        if not al:
            return "No open battery-degradation alerts right now.", []
        lines = [f"**{len(al)} vehicles have open battery-degradation alerts:**"]
        for a in al[:8]:
            lines.append(f"- `{a['vin']}` — {a['cause']}")
        actions += [f"Schedule a battery capacity test for {a['vin']}." for a in al[:3]]
        if pr:
            lines.append(f"\nHighest predicted failure risk overall: `{pr[0]['vin']}` at {round(float(pr[0]['failure_prob']) * 100)}%.")
        return "\n".join(lines), actions

    if i == "engine_faults":
        al = R["get_recent_alerts"]["alerts"]
        kb = R.get("search_knowledge_base", {}).get("documents", [])
        if not al:
            return "No vehicle currently has repeated diagnostic trouble codes.", []
        per: dict[str, list[dict]] = {}
        for a in al:
            per.setdefault(a["vin"], []).append(a)
        lines = [f"**{len(per)} vehicles have repeated engine fault codes:**"]
        for vin, items in per.items():
            codes = sorted({(x["details"] or {}).get("code", "?") for x in items})
            lines.append(f"- `{vin}` — codes {', '.join(codes)} ({items[0]['cause']})")
            actions.append(f"Book a diagnostic inspection for {vin} ({', '.join(codes)}).")
        if kb:
            lines.append(f"\nGuidance ({kb[0]['title']}): {kb[0]['content'][:220]}")
        return "\n".join(lines), actions[:5]

    if i == "recommend":
        s = R["get_fleet_summary"]["summary"]
        crit = R["get_recent_alerts"]["alerts"]
        pr = R["get_maintenance_predictions"]["predictions"]
        idle = R["calculate_idle_cost"]
        lines = ["**Recommended priorities (ranked by risk to safety and uptime):**"]
        n = 1
        if crit:
            lines.append(f"{n}. Resolve {s['open_alerts']['critical']} critical alerts — start with `{crit[0]['vin']}`: {crit[0]['cause']}")
            actions.append(crit[0]["recommended_action"])
            n += 1
        if pr:
            lines.append(f"{n}. Service the highest-risk vehicles: " + ", ".join(f"`{p['vin']}` ({round(float(p['failure_prob']) * 100)}%)" for p in pr[:3]))
            actions.append(f"Schedule inspections for {', '.join(p['vin'] for p in pr[:3])}.")
            n += 1
        if s["safety"]["high_risk_vehicles"]:
            lines.append(f"{n}. Coach drivers: {s['safety']['high_risk_vehicles']} vehicles have a risk score ≥ 70 (fleet safety score {s['safety']['score']}).")
            actions.append("Schedule defensive-driving coaching for high-risk drivers.")
            n += 1
        if idle["idle_hours"]:
            lines.append(f"{n}. Cut idling: {idle['idle_hours']} idle hours in {idle['days']} days cost about {idle['cost_usd']} USD; a {idle['reduction_pct']:.0f}% reduction saves about {idle['potential_saving_usd']} USD.")
            actions.append("Introduce an idle-shutdown policy.")
        if n == 1:
            lines.append("No critical items — fleet health averages " + str(s["fleet_health"]["avg_score"]) + ".")
        return "\n".join(lines), actions

    if i == "idle_savings":
        c = R["calculate_idle_cost"]
        al = R["get_recent_alerts"]["alerts"]
        lines = [f"Over the last {c['days']} days, **{c['idle_hours']} h** of idling across {c['idling_vehicles']} vehicles burned about **{c['fuel_litres']} L** "
                 f"(≈ **{c['cost_usd']} USD**).",
                 f"Cutting idling by {c['reduction_pct']:.0f}% would save about **{c['potential_saving_usd']} USD** in this period "
                 f"(≈ {c['annualised_saving_usd']} USD per year).",
                 f"Assumptions: {c['assumptions']['idle_fuel_lph']} L/h at {c['assumptions']['fuel_price_per_litre']} USD/L."]
        if al:
            lines.append(f"{len(al)} vehicles currently have open excessive-idling alerts, e.g. `{al[0]['vin']}`: {al[0]['cause']}")
        actions += ["Adopt an idle-shutdown policy (engine off after 5 minutes parked).", "Review routes/depot waiting times for the most frequently idling vehicles."]
        return "\n".join(lines), actions

    if i == "driver_risk":
        ds = R["get_driver_risk"]["drivers"]
        if not ds:
            return "No driver-risk data yet.", []
        lines = ["**Highest driver-risk vehicles:**"]
        for k, d in enumerate(ds, 1):
            who = f" (driver {d['driver']})" if d.get("driver") else ""
            lines.append(f"{k}. `{d['vin']}`{who} — risk {d['risk_score']}: {d['harsh_brake_30m']} harsh brakes, {d['harsh_accel_30m']} harsh accelerations, {d['speeding_30m']} speeding samples (30 min)")
        top = ds[0]
        if top["risk_score"] >= 70:
            actions.append(f"Review the latest trip of {top['vin']} with the driver and assign coaching.")
        return "\n".join(lines), actions

    if i == "nearby":
        near = R.get("find_nearby_vehicle")
        if not near or not near.get("found"):
            return (near or {}).get("message", "Please include a VIN so I can find vehicles near it."), []
        lines = ["**Closest vehicles:**"] + [f"- `{v['vin']}` — {v['est_road_km']} km by road (~{v['eta_min']} min), {v['status']}, health {v['health']}" for v in near["vehicles"]]
        actions.append(f"Dispatch {near['vehicles'][0]['vin']} (ETA {near['vehicles'][0]['eta_min']} min).")
        return "\n".join(lines), actions

    if i == "schedule":
        r = R["recommend_maintenance_plan"]
        if not r["scheduled"]:
            return "No vehicle currently needs workshop time.", []
        lines = [f"With {r['capacity_hours']} workshop hours, the optimal plan (0/1 knapsack, {r['candidates']} candidates) services **{len(r['scheduled'])} vehicles** "
                 f"using {r['total_hours']} h and avoids ≈ {r['expected_failures_avoided']} expected failures:"]
        lines += [f"- `{s['vin']}` — {s['hours']} h, failure risk {round(s['failure_prob'] * 100)}%" for s in r["scheduled"][:12]]
        actions.append(f"Book {len(r['scheduled'])} workshop slots as listed.")
        return "\n".join(lines), actions

    if i == "alerts":
        al = R["get_recent_alerts"]["alerts"]
        s = R["get_fleet_summary"]["summary"]
        lines = [f"**{s['open_alerts']['total']} open alerts** ({s['open_alerts']['critical']} critical, {s['open_alerts']['warning']} warning). Most recent:"]
        lines += [f"- [{a['severity']}] `{a['vin']}` {a['type']} — {a['cause']}" for a in al[:8]]
        actions += [a["recommended_action"] for a in al[:2]]
        return "\n".join(lines), actions

    if i == "knowledge":
        kb = R["search_knowledge_base"]
        if not kb["documents"]:
            return "I found no relevant knowledge-base articles.", []
        lines = [f"**{d['title']}** ({d['category']}, match {d['score']}): {d['content']}" for d in kb["documents"][:2]]
        return "\n\n".join(lines), []

    if i == "vehicle_status":
        if "get_vehicle_health" not in R:
            return _overview(R)
        return _vehicle(plan, R)

    if i in ("create_alert", "ack_alert"):
        return "", []
    return _overview(R)


def _overview(R: dict) -> tuple[str, list[str]]:  # noqa: N803
    s = R["get_fleet_summary"]["summary"]
    crit = R.get("get_recent_alerts", {}).get("alerts", [])
    lines = [f"Fleet overview: **{s['total_vehicles']}** vehicles, **{s['active_vehicles']}** active now, **{s['open_alerts']['total']}** open alerts "
             f"({s['open_alerts']['critical']} critical), average health **{s['fleet_health']['avg_score']}**, safety score **{s['safety']['score']}**."]
    lines += [f"- [{a['severity']}] `{a['vin']}` {a['cause']}" for a in crit[:5]]
    return "\n".join(lines), [a["recommended_action"] for a in crit[:2]]


def _vehicle(plan: Plan, R: dict) -> tuple[str, list[str]]:  # noqa: N803
    h = R["get_vehicle_health"]
    if not h.get("found"):
        return h["message"], []
    v, hs = h["vehicle"], h.get("health") or {}
    lines = [f"`{h['vin']}` — {v['make']} {v['model']} {v['model_year']} ({v['fuel_type']}), fleet {v['fleet']}."]
    if hs.get("health_score") is not None:
        factors = hs.get("factors") or {}
        why = ", ".join(f"{k.replace('_', ' ')} (-{round(float(x))})" for k, x in factors.items()) or "no active penalties"
        lines.append(f"Health score **{hs['health_score']}/100** — {why}. Driver risk {hs.get('risk_score')}.")
    actions: list[str] = []
    for a in h.get("open_alerts", [])[:3]:
        lines.append(f"- Open [{a['severity']}] {a['type']}: {a['cause']}")
        actions.append(a["recommended_action"])
    t = R.get("search_telemetry")
    if t and t.get("samples"):
        sig = t["signals"]
        bits = [f"{k.replace('_', ' ')} {v_['min']}–{v_['max']}" for k, v_ in list(sig.items())[:4]]
        lines.append(f"Last {t['window_minutes']} min ({t['samples']} samples): " + "; ".join(bits) + (f"; DTCs {t['dtc_counts']}" if t["dtc_counts"] else ""))
    p = h.get("prediction")
    if p:
        lines.append(f"Predicted 14-day failure probability: **{round(float(p['failure_prob']) * 100)}%** ({_fmt_factors(p['top_factors'])}).")
    kb = R.get("search_knowledge_base", {}).get("documents", [])
    if kb:
        lines.append(f"Relevant guidance — {kb[0]['title']}: {kb[0]['content'][:200]}")
    return "\n".join(lines), actions
