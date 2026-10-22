"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from cvi.api.context import get_ctx
from cvi.api.middleware import SecurityMiddleware
from cvi.api.routers import admin, agent, alerts, analytics, auth, vehicles
from cvi.api.routers import map as map_router
from cvi.common.config import get_settings
from cvi.common.logging import setup_logging
from cvi.storage.pg import ping

logger = logging.getLogger("api")


def create_app() -> FastAPI:
    s = get_settings()
    setup_logging("api", s.log_level)
    app = FastAPI(title="Connected Vehicle Intelligence API", version="1.0.0",
                  docs_url="/docs" if s.env != "production" else None, redoc_url=None)
    app.add_middleware(SecurityMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in s.cors_origins.split(",") if o.strip()],
                       allow_methods=["GET", "POST", "PATCH"], allow_headers=["Authorization", "Content-Type"], allow_credentials=False,
                       max_age=600)
    for r in (auth.router, vehicles.router, alerts.router, analytics.router, map_router.router, agent.router, admin.router):
        app.include_router(r)

    @app.get("/health/live", tags=["ops"])
    def live():
        return {"status": "up"}

    @app.get("/health/ready", tags=["ops"])
    def ready(response: Response):
        """Readiness = can serve requests. Postgres is required; Redis/Mongo/broker down => 'degraded' (still ready)."""
        ctx = get_ctx()
        checks = {"postgres": ping(ctx.pool), "redis": ctx.state.ping(), "mongo": ctx.raw.ping(), "broker": ctx.broker.healthy()}
        if not checks["postgres"]:
            response.status_code = 503
        return {"status": "ready" if all(checks.values()) else "degraded" if checks["postgres"] else "unavailable", "checks": checks}

    @app.get("/metrics", tags=["ops"])
    def prom():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    if s.otlp_endpoint:
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=Resource.create({"service.name": "cvi-api"}))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=s.otlp_endpoint + "/v1/traces")))
            trace.set_tracer_provider(provider)
            FastAPIInstrumentor.instrument_app(app)
        except Exception as e:  # noqa: BLE001
            logger.warning("tracing disabled: %s", e)
    return app


