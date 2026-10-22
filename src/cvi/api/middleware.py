"""HTTP middleware: security headers, request metrics, body limit, rate limiting (Redis token bucket with in-memory
fallback so a Redis outage degrades protection instead of availability)."""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from cvi.api.context import get_ctx
from cvi.common import metrics
from cvi.common.config import get_settings

logger = logging.getLogger("api")
MAX_BODY = 1_000_000

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


class _LocalBucket:
    def __init__(self):
        self.b: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, per_minute: int) -> bool:
        now = time.monotonic()
        tokens, ts = self.b.get(key, (float(per_minute), now))
        tokens = min(per_minute, tokens + (now - ts) * per_minute / 60.0)
        ok = tokens >= 1
        self.b[key] = (tokens - 1 if ok else tokens, now)
        if len(self.b) > 50_000:
            self.b.clear()
        return ok


_local = _LocalBucket()


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        s = get_settings()
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        t0 = time.perf_counter()
        route_label = request.url.path
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"detail": "request body too large"}, 413)
        status = 500
        try:
            if not route_label.startswith(("/health", "/metrics", "/docs", "/openapi")):
                ident = request.headers.get("authorization", "")[-24:] or (request.client.host if request.client else "anon")
                limit = s.rate_limit_per_minute
                try:
                    ok = get_ctx().state.allow(f"api:{ident}", limit)
                except Exception:  # noqa: BLE001 - Redis down: fall back to a per-process bucket
                    ok = _local.allow(ident, limit)
                if not ok:
                    status = 429
                    return JSONResponse({"detail": "rate limit exceeded"}, 429, headers={"Retry-After": "5", **SECURITY_HEADERS})
            response = await call_next(request)
            status = response.status_code
            for k, v in SECURITY_HEADERS.items():
                response.headers.setdefault(k, v)
            response.headers["X-Request-ID"] = rid
            route = request.scope.get("route")
            route_label = getattr(route, "path", route_label)
            return response
        finally:
            dur = time.perf_counter() - t0
            route = request.scope.get("route")
            label = getattr(route, "path", "unmatched")
            metrics.api_request_latency.labels(request.method, label, str(status)).observe(dur)
            metrics.api_requests_total.labels(request.method, label, str(status)).inc()
