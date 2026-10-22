from __future__ import annotations

import base64
import datetime as dt
import logging
from typing import Any

import orjson
from fastapi import Request

from cvi.api.context import AppContext
from cvi.api.security import Principal
from cvi.storage.pg import append_audit, tenant_tx

logger = logging.getLogger("api")


def encode_cursor(*parts: Any) -> str:
    return base64.urlsafe_b64encode(orjson.dumps([p.isoformat() if isinstance(p, dt.datetime) else p for p in parts])).decode()


def decode_cursor(cur: str | None) -> list | None:
    if not cur:
        return None
    try:
        return orjson.loads(base64.urlsafe_b64decode(cur.encode()))
    except Exception:  # noqa: BLE001
        return None


def audit(ctx: AppContext, p: Principal | None, action: str, resource: str, resource_id: str | int | None, request: Request | None = None,
          details: dict | None = None, tenant_id: int | None = None) -> None:
    """Append a hash-chained audit record. Writes (mutations) must fail loudly; callers decide whether to swallow."""
    tid = tenant_id if tenant_id is not None else (p.tenant_id if p else None)
    row = {"actor_id": p.user_id if p else None, "action": action, "resource": resource,
           "resource_id": None if resource_id is None else str(resource_id),
           "ip": request.client.host if request and request.client else None, "details": details or {}}
    if tid is None:
        raise ValueError("audit requires a tenant context")
    with tenant_tx(ctx.pool, tid) as c:
        append_audit(c, "audit_log", tid, row)


def jsonable(rows: list[dict]) -> list[dict]:
    return orjson.loads(orjson.dumps(rows, default=str))
