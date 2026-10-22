from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from cvi.api.common import audit, decode_cursor, encode_cursor, jsonable
from cvi.api.context import AppContext, dep_ctx
from cvi.api.security import Principal, require
from cvi.storage.pg import tenant_tx
from cvi.storage.vector import similar_incidents

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])

FEED_SQL = """
SELECT a.id, a.vehicle_id, v.vin, a.alert_type_code AS type, t.title, a.severity, a.status, a.confidence, a.cause,
       a.recommended_action, a.details, a.event_ts, a.created_at
FROM alerts a
JOIN vehicles v ON v.id = a.vehicle_id
JOIN alert_types t ON t.code = a.alert_type_code
WHERE a.tenant_id = %(tenant)s
  AND (%(status)s::text IS NULL OR a.status = %(status)s)
  AND (%(severity)s::text IS NULL OR a.severity = %(severity)s)
  AND (%(vehicle)s::bigint IS NULL OR a.vehicle_id = %(vehicle)s)
  AND (%(cur_ts)s::timestamptz IS NULL OR (a.created_at, a.id) < (%(cur_ts)s, %(cur_id)s))
ORDER BY a.created_at DESC, a.id DESC
LIMIT %(limit)s
"""


@router.get("")
def list_alerts(status: Literal["open", "acknowledged", "resolved"] | None = None, severity: Literal["info", "warning", "critical"] | None = None,
                vehicle_id: int | None = None, limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
                p: Principal = Depends(require("alerts:read")), ctx: AppContext = Depends(dep_ctx)):
    """Keyset pagination on (created_at, id): served by alerts_open_severity_idx / alerts_tenant_created_idx."""
    cur = decode_cursor(cursor)
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rows = c.execute(FEED_SQL, {"tenant": p.tenant_id, "status": status, "severity": severity, "vehicle": vehicle_id,
                                    "cur_ts": cur[0] if cur else None, "cur_id": cur[1] if cur else None, "limit": limit + 1}).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if more else None
    return {"items": jsonable(rows), "next_cursor": nxt}


@router.get("/{alert_id}")
def alert_detail(alert_id: int, p: Principal = Depends(require("alerts:read")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        row = c.execute(FEED_SQL.replace("a.tenant_id = %(tenant)s", "a.tenant_id = %(tenant)s AND a.id = %(alert)s"),
                        {"tenant": p.tenant_id, "alert": alert_id, "status": None, "severity": None, "vehicle": None,
                         "cur_ts": None, "cur_id": None, "limit": 1}).fetchone()
        if not row:
            raise HTTPException(404, "alert not found")
        similar = similar_incidents(c, p.tenant_id, f"{row['type']} {row['cause']}", 4)
    return {"alert": jsonable([row])[0], "similar_incidents": jsonable([s for s in similar if s["alert_id"] != alert_id][:3])}


class AlertUpdate(BaseModel):
    status: Literal["acknowledged", "resolved"]


@router.patch("/{alert_id}")
def update_alert(alert_id: int, body: AlertUpdate, request: Request, p: Principal = Depends(require("alerts:write")),
                 ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        row = c.execute("UPDATE alerts SET status = %s, acknowledged_by = %s, resolved_at = CASE WHEN %s = 'resolved' THEN now() ELSE resolved_at END "
                        "WHERE id = %s AND tenant_id = %s RETURNING id, status", [body.status, p.user_id, body.status, alert_id, p.tenant_id]).fetchone()
    if not row:
        raise HTTPException(404, "alert not found")
    audit(ctx, p, f"alert_{body.status}", "alert", alert_id, request)
    return row


_ = dt  # re-exported for type checkers
