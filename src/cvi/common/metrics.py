"""Prometheus metrics shared by all services (names match the hackathon observability list)."""

from prometheus_client import Counter, Gauge, Histogram

LAT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10)

events_received_total = Counter("events_received_total", "Telemetry events received", ["service"])
events_processed_total = Counter("events_processed_total", "Telemetry events processed", ["service"])
events_failed_total = Counter("events_failed_total", "Telemetry events failed", ["service", "reason"])
events_duplicate_total = Counter("events_duplicate_total", "Duplicate events dropped")
events_late_total = Counter("events_late_total", "Events older than allowed lateness")
events_out_of_order_total = Counter("events_out_of_order_total", "Out-of-order events within lateness")
dlq_total = Counter("dlq_messages_total", "Messages routed to the dead-letter topic", ["reason"])
alerts_generated_total = Counter("alerts_generated_total", "Alerts generated", ["type", "severity"])
processing_latency = Histogram("processing_latency_seconds", "Event ingest-to-processed latency", buckets=LAT_BUCKETS)
alert_latency = Histogram("alert_latency_seconds", "Event-time to alert-persisted latency", buckets=LAT_BUCKETS)
batch_duration = Histogram("batch_duration_seconds", "Stream batch processing time", buckets=LAT_BUCKETS)
consumer_lag = Gauge("consumer_lag", "Consumer lag (messages)", ["topic"])
vehicles_active = Gauge("vehicles_active", "Vehicles seen in the last 5 minutes")
circuit_state = Gauge("circuit_breaker_open", "1 when the breaker is open", ["name"])
backpressure_pauses_total = Counter("backpressure_pauses_total", "Times consumption was paused due to back-pressure")
api_request_latency = Histogram(
    "api_request_latency_seconds", "API request latency", ["method", "route", "status"], buckets=LAT_BUCKETS
)
api_requests_total = Counter("api_requests_total", "API requests", ["method", "route", "status"])
agent_tool_calls_total = Counter("agent_tool_calls_total", "Agent tool invocations", ["tool", "outcome"])
