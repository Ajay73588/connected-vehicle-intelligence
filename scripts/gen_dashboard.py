#!/usr/bin/env python
"""Generate the provisioned Grafana dashboard (infrastructure/observability/grafana/dashboards/cvi-overview.json)."""
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prom"}
panels = []


def panel(title, exprs, x, y, w=8, h=7, unit="short", kind="timeseries", legend=None):
    panels.append({
        "id": len(panels) + 1, "title": title, "type": kind, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [{"expr": e, "legendFormat": (legend[i] if legend else "{{__name__}}"), "refId": chr(65 + i), "datasource": DS} for i, e in enumerate(exprs)],
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
    })


q = lambda p, m, by="le": f"histogram_quantile({p}, sum by ({by}) (rate({m}_bucket[2m])))"  # noqa: E731

panel("Ingestion rate (events/s)", ["sum(rate(events_received_total[1m]))", "sum(rate(events_processed_total{service=\"validator\"}[1m]))",
      "sum(rate(events_processed_total{service=\"stream\"}[1m]))"], 0, 0, legend=["received (edge)", "validated", "processed (stream)"], unit="ops")
panel("Consumer lag (messages)", ["sum by (topic) (consumer_lag)"], 8, 0, legend=["{{topic}}"])
panel("Event processing latency (ingest → processed)", [q(0.5, "processing_latency_seconds"), q(0.95, "processing_latency_seconds"), q(0.99, "processing_latency_seconds")],
      16, 0, unit="s", legend=["p50", "p95", "p99"])
panel("Alert latency (ingest → alert persisted)  — SLO p99 < 5 s", [q(0.5, "alert_latency_seconds"), q(0.95, "alert_latency_seconds"), q(0.99, "alert_latency_seconds")],
      0, 7, unit="s", legend=["p50", "p95", "p99"])
panel("Alerts generated / min", ["sum by (type) (increase(alerts_generated_total[1m]))"], 8, 7, legend=["{{type}}"])
panel("Failed / DLQ events by reason (/s)", ["sum by (reason) (rate(dlq_messages_total[1m]))"], 16, 7, legend=["{{reason}}"], unit="ops")
panel("API latency p95 / p99 by route", ["histogram_quantile(0.95, sum by (le, route) (rate(api_request_latency_seconds_bucket[2m])))",
      "histogram_quantile(0.99, sum by (le, route) (rate(api_request_latency_seconds_bucket[2m])))"], 0, 14, w=12, unit="s", legend=["p95 {{route}}", "p99 {{route}}"])
panel("API error rate (5xx) and 429s", ["sum(rate(api_requests_total{status=~\"5..\"}[1m]))", "sum(rate(api_requests_total{status=\"429\"}[1m]))"], 12, 14, w=6, legend=["5xx", "429 rate-limited"], unit="ops")
panel("Active vehicles (5 min)", ["sum(vehicles_active)"], 18, 14, w=6, kind="stat")
panel("Duplicates / out-of-order / late (/s)", ["rate(events_duplicate_total[1m])", "rate(events_out_of_order_total[1m])", "rate(events_late_total[1m])"], 0, 21,
      legend=["duplicates dropped", "out of order", "late (> lateness)"], unit="ops")
panel("Circuit breakers (1 = open) & back-pressure pauses", ["max by (name) (circuit_breaker_open)", "increase(backpressure_pauses_total[5m])"], 8, 21,
      legend=["breaker {{name}}", "back-pressure pauses (5m)"])
panel("Agent tool calls by outcome", ["sum by (tool, outcome) (increase(agent_tool_calls_total[5m]))"], 16, 21, legend=["{{tool}} {{outcome}}"])
panel("Service health (up)", ["up"], 0, 28, w=24, h=5, legend=["{{job}} {{instance}}"])

dash = {"uid": "cvi-overview", "title": "CVI — Pipeline & API overview", "schemaVersion": 39, "version": 1, "refresh": "5s", "time": {"from": "now-30m", "to": "now"},
        "tags": ["cvi"], "panels": panels, "templating": {"list": []}}
out = Path(__file__).resolve().parents[1] / "infrastructure" / "observability" / "grafana" / "dashboards" / "cvi-overview.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dash, indent=2))
print("wrote", out)
