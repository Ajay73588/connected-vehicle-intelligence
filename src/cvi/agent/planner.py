"""Intent classification + tool selection. Deterministic (keyword scoring + entity extraction): it never needs an LLM,
so the agent works offline, is reproducible and cheap to test. An LLM, when configured, only rewrites the final answer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
NUM_RE = re.compile(r"\b(\d{1,3})\b")

INTENTS: dict[str, list[str]] = {
    "maintenance_risk": ["maintenance risk", "highest maintenance", "predicted failure", "likely to fail", "predictive", "maintenance prediction", "at risk", "fail soon", "maintenance"],
    "utilization": ["utilization", "utilisation", "utilized", "utilised", "fleet usage", "usage down", "less active"],
    "battery": ["battery", "soh", "state of health", "degradation", "ev health"],
    "engine_faults": ["engine fault", "repeated fault", "repeated engine", "dtc", "diagnostic", "trouble code", "check engine", "misfire"],
    "recommend": ["what actions", "what should", "recommend", "next steps", "prioritise", "prioritize", "action plan", "should the fleet manager"],
    "idle_savings": ["idling", "idle", "save money", "fuel waste", "reduce idling"],
    "driver_risk": ["driver", "dangerous", "unsafe", "harsh", "speeding", "safety score"],
    "nearby": ["nearest", "nearby", "closest", "dispatch", "near "],
    "schedule": ["schedule", "workshop", "service plan", "maintenance plan", "capacity"],
    "alerts": ["alert", "critical", "incident", "what is wrong", "problems"],
    "knowledge": ["how do i", "how to", "what does", "meaning of", "runbook", "procedure", "manual"],
    "create_alert": ["create an alert", "raise an alert", "open an alert", "flag vehicle", "create alert"],
    "ack_alert": ["acknowledge alert", "ack alert", "acknowledge the alert"],
}


@dataclass
class ToolCall:
    tool: str
    args: dict = field(default_factory=dict)


@dataclass
class Plan:
    intent: str
    calls: list[ToolCall]
    entities: dict


def classify(message: str) -> tuple[str, dict]:
    m = message.lower()
    ent: dict = {}
    vin = VIN_RE.search(message.upper())
    if vin:
        ent["vin"] = vin.group(0)
    num = [int(x) for x in NUM_RE.findall(m) if int(x) > 0 and x != "17"]
    if num:
        ent["n"] = num[0]
    scores = {i: sum(len(k.split()) + (2 if k in m else 0) for k in kws if k in m) for i, kws in INTENTS.items()}
    # explicit write intents win; a VIN with no stronger intent means "tell me about this vehicle"
    for strong in ("create_alert", "ack_alert"):
        if scores[strong]:
            return strong, ent
    best = max(scores, key=scores.__getitem__)
    if "vin" in ent and any(p in m for p in ("status of", "how is", "tell me about", "explain", "why is", "health of", "what is wrong")):
        return "vehicle_status", ent
    if scores[best] == 0:
        return ("vehicle_status" if "vin" in ent else "overview"), ent
    if "vin" in ent and best in ("alerts", "vehicle_status", "engine_faults", "battery", "driver_risk", "maintenance_risk"):
        return "vehicle_status", ent
    return best, ent


def plan(message: str) -> Plan:
    intent, ent = classify(message)
    n = min(ent.get("n", 5), 20)
    vin = ent.get("vin")
    c: list[ToolCall] = []
    if intent == "maintenance_risk":
        c = [ToolCall("get_maintenance_predictions", {"limit": n, "min_prob": 0.3}), ToolCall("get_fleet_summary")]
    elif intent == "utilization":
        c = [ToolCall("get_utilization_analysis"), ToolCall("get_fleet_summary"), ToolCall("get_recent_alerts", {"severity": "critical", "limit": 5})]
    elif intent == "battery":
        c = [ToolCall("get_recent_alerts", {"alert_type": "battery_degradation", "limit": 10}), ToolCall("get_maintenance_predictions", {"limit": 5, "min_prob": 0.3})]
        if vin:
            c = [ToolCall("get_vehicle_health", {"vin": vin}), ToolCall("search_telemetry", {"vin": vin, "minutes": 60})]
    elif intent == "engine_faults":
        c = [ToolCall("get_recent_alerts", {"alert_type": "repeated_dtc", "limit": 20}), ToolCall("search_knowledge_base", {"query": "repeated engine diagnostic trouble code misfire"})]
    elif intent == "recommend":
        c = [ToolCall("get_fleet_summary"), ToolCall("get_recent_alerts", {"severity": "critical", "limit": 5}),
             ToolCall("get_maintenance_predictions", {"limit": 5, "min_prob": 0.4}), ToolCall("calculate_idle_cost", {"days": 7, "reduction_pct": 30})]
    elif intent == "idle_savings":
        c = [ToolCall("calculate_idle_cost", {"days": 7, "reduction_pct": 30}), ToolCall("get_recent_alerts", {"alert_type": "excessive_idling", "limit": 10}),
             ToolCall("search_knowledge_base", {"query": "reduce idling policy fuel cost"})]
    elif intent == "driver_risk":
        c = [ToolCall("get_driver_risk", {"limit": n}), ToolCall("get_recent_alerts", {"alert_type": "dangerous_driving", "limit": 5})]
    elif intent == "nearby":
        c = [ToolCall("find_nearby_vehicle", {"vin": vin, "k": min(n, 5) if "n" in ent else 3})] if vin else []
    elif intent == "schedule":
        c = [ToolCall("recommend_maintenance_plan", {"capacity_hours": ent.get("n", 24) if ent.get("n", 24) >= 4 else 24})]
    elif intent == "alerts":
        c = [ToolCall("get_recent_alerts", {"limit": 10}), ToolCall("get_fleet_summary")]
    elif intent == "knowledge":
        c = [ToolCall("search_knowledge_base", {"query": message[:300]})]
    elif intent == "vehicle_status":
        c = [ToolCall("get_vehicle_health", {"vin": vin}), ToolCall("search_telemetry", {"vin": vin, "minutes": 30}),
             ToolCall("search_knowledge_base", {"query": message[:200]})] if vin else [ToolCall("get_fleet_summary"), ToolCall("get_recent_alerts", {"limit": 5})]
    elif intent == "create_alert":
        c = [ToolCall("create_alert", {"vin": vin, "title": _title(message), "severity": "critical" if "critical" in message.lower() else "warning"})] if vin else []
    elif intent == "ack_alert":
        aid = re.search(r"alert\s*#?(\d+)", message.lower())
        c = [ToolCall("acknowledge_alert", {"alert_id": int(aid.group(1))})] if aid else []
    else:  # overview
        c = [ToolCall("get_fleet_summary"), ToolCall("get_recent_alerts", {"severity": "critical", "limit": 5})]
    return Plan(intent, c, ent)


def _title(message: str) -> str:
    t = re.sub(VIN_RE, "", message.upper()).strip(" :,-").lower()
    t = re.sub(r"(create|raise|open|flag)( an?)? alert( for| on)?", "", t).strip(" :,-")
    return (t or "manual follow-up")[:120]
