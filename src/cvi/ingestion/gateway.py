"""IoT edge: HTTPS batch endpoint + MQTT subscriber. Both only do authentication, size limits and a durable append to
`telemetry.raw` (so slow validation/processing never blocks devices). Overload -> 503 + Retry-After (back-pressure)."""

from __future__ import annotations

import hmac
import logging
import time

import orjson
from fastapi import FastAPI, Header, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from cvi.common import metrics
from cvi.common.config import get_settings
from cvi.common.logging import setup_logging
from cvi.common.resilience import CircuitBreaker, CircuitOpenError
from cvi.messaging.base import TOPIC_RAW, Broker
from cvi.messaging.factory import get_broker

logger = logging.getLogger("edge")
MAX_BATCH = 5000
MAX_BODY = 8 * 1024 * 1024


def create_edge_app(broker: Broker | None = None) -> FastAPI:
    s = get_settings()
    setup_logging("edge", s.log_level)
    broker = broker or get_broker()
    breaker = CircuitBreaker("broker", failure_threshold=5, reset_timeout=3.0)
    app = FastAPI(title="CVI IoT edge", docs_url=None, redoc_url=None)

    def publish(payloads: list[tuple[str | None, bytes]]) -> None:
        ts = str(int(time.time() * 1000))

        def go():
            for key, value in payloads:
                broker.produce(TOPIC_RAW, key, value, {"ingest_ts": ts})
        breaker.call(go)

    @app.post("/v1/telemetry", status_code=202)
    async def ingest(request: Request, x_device_key: str | None = Header(default=None)):
        if not x_device_key or not hmac.compare_digest(x_device_key, s.ingest_api_key):
            raise HTTPException(401, "invalid device key")
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "payload too large")
        try:
            data = orjson.loads(body)
        except orjson.JSONDecodeError:
            raise HTTPException(400, "body must be JSON") from None
        items = data if isinstance(data, list) else [data]
        if len(items) > MAX_BATCH:
            raise HTTPException(413, f"batch limited to {MAX_BATCH} events")
        payloads = [((it.get("vin") if isinstance(it, dict) else None), orjson.dumps(it)) for it in items]
        metrics.events_received_total.labels("edge-http").inc(len(items))
        try:
            publish(payloads)
        except (CircuitOpenError, ConnectionError, BufferError) as e:
            raise HTTPException(503, "ingestion temporarily unavailable", headers={"Retry-After": "2"}) from e
        return {"accepted": len(items)}

    @app.get("/health/live")
    def live():
        return {"status": "up"}

    @app.get("/health/ready")
    def ready(response: Response):
        ok = broker.healthy()
        if not ok:
            response.status_code = 503
        return {"status": "ready" if ok else "broker-unavailable"}

    @app.get("/metrics")
    def prom():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.state.broker = broker
    return app


def start_mqtt_bridge(broker: Broker) -> object:
    """Subscribe to telemetry/<tenant>/<vin> and append to telemetry.raw (QoS 1)."""
    import paho.mqtt.client as mqtt

    s = get_settings()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cvi-edge")

    def on_connect(c, _u, _f, rc, _p=None):
        c.subscribe("telemetry/+/+", qos=1)
        logger.info("mqtt connected (rc=%s)", rc)

    def on_message(_c, _u, msg):
        vin = msg.topic.rsplit("/", 1)[-1]
        metrics.events_received_total.labels("edge-mqtt").inc()
        broker.produce(TOPIC_RAW, vin, msg.payload, {"ingest_ts": str(int(time.time() * 1000))})

    client.on_connect, client.on_message = on_connect, on_message
    client.connect_async(s.mqtt_host, s.mqtt_port)
    client.loop_start()
    return client


def main() -> None:
    import uvicorn

    s = get_settings()
    app = create_edge_app()
    for _ in range(30):
        try:
            app.state.broker.ensure_topics()
            break
        except Exception as e:  # noqa: BLE001
            logger.warning("waiting for broker: %s", e)
            time.sleep(2)
    start_mqtt_bridge(app.state.broker)
    host = "0.0.0.0"  # nosec B104 - container entrypoint; exposure is controlled by the orchestrator / network policy
    uvicorn.run(app, host=host, port=s.ingestion_port, log_level="warning")


if __name__ == "__main__":
    main()
