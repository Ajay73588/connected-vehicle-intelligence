from __future__ import annotations

import time
from typing import Literal

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from cvi.api.common import audit, jsonable
from cvi.api.context import AppContext, dep_ctx
from cvi.api.security import Principal, require
from cvi.lifecycle.erasure import erase_driver, erase_vehicle
from cvi.messaging.base import TOPIC_RAW
from cvi.simulator.scenarios import DESCRIPTIONS, SCENARIOS
from cvi.storage.pg import tenant_tx, verify_chain

router = APIRouter(prefix="/api/v1", tags=["admin"])


@router.get("/admin/audit")
def audit_log(limit: int = Query(100, ge=1, le=500), p: Principal = Depends(require("admin:audit")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rows = c.execute("SELECT id, actor_id, action, resource, resource_id, ip, details, ts FROM audit_log WHERE tenant_id = %s ORDER BY id DESC LIMIT %s",
                         [p.tenant_id, limit]).fetchall()
        ok, n = verify_chain(c, "audit_log", p.tenant_id)
    return {"items": jsonable(rows), "chain_valid": ok, "chain_length": n}


class ErasureIn(BaseModel):
    subject_type: Literal["vehicle", "driver"]
    subject_ref: str = Field(min_length=1, max_length=40, pattern="^[A-Za-z0-9]+$")


@router.post("/admin/erasure")
def erasure(body: ErasureIn, request: Request, p: Principal = Depends(require("admin:erasure")), ctx: AppContext = Depends(dep_ctx)):
    """Right-to-erasure workflow: records the request, deletes across hot/warm/cold tiers, stores a report, audits it."""
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rid = c.execute("INSERT INTO erasure_requests (tenant_id, subject_type, subject_ref, requested_by) VALUES (%s,%s,%s,%s) RETURNING id",
                        [p.tenant_id, body.subject_type, body.subject_ref, p.user_id]).fetchone()["id"]
    audit(ctx, p, "erasure_requested", body.subject_type, body.subject_ref, request)
    try:
        wp = ctx.worker_pool or ctx.pool
        report = (erase_vehicle(wp, ctx.state, ctx.raw, ctx.objects, p.tenant_id, p.tenant_slug, body.subject_ref.upper())
                  if body.subject_type == "vehicle" else erase_driver(wp, p.tenant_id, int(body.subject_ref)))
        status = "completed"
    except (LookupError, ValueError) as e:
        report, status = {"error": str(e)}, "failed"
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        c.execute("UPDATE erasure_requests SET status = %s, completed_at = now(), report = %s::jsonb WHERE id = %s",
                  [status, orjson.dumps(report).decode(), rid])
    audit(ctx, p, f"erasure_{status}", body.subject_type, body.subject_ref, request, {"report": report})
    if status == "failed":
        raise HTTPException(404, report["error"])
    return {"request_id": rid, "status": status, "report": report}


@router.get("/admin/erasure")
def erasure_list(p: Principal = Depends(require("admin:erasure")), ctx: AppContext = Depends(dep_ctx)):
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rows = c.execute("SELECT id, subject_type, subject_ref, status, requested_at, completed_at, report FROM erasure_requests "
                         "WHERE tenant_id = %s ORDER BY id DESC LIMIT 50", [p.tenant_id]).fetchall()
    return {"items": jsonable(rows)}


# ------------------------------------------------------------------------------------------------ demo scenarios
@router.get("/demo/scenarios")
def scenarios(p: Principal = Depends(require("demo:run"))):
    return {"items": [{"name": k, "description": v} for k, v in DESCRIPTIONS.items()]}


@router.post("/demo/scenarios/{name}")
def run_demo(name: str, request: Request, p: Principal = Depends(require("demo:run")), ctx: AppContext = Depends(dep_ctx)):
    """Injects the deterministic scenario's events at the front door (telemetry.raw) so they flow through the REAL pipeline:
    validation -> stream processing -> detectors -> alerts. Scenario vehicles belong to fixed tenants, so run it as that
    tenant's user (the response says which)."""
    import random

    from cvi.simulator.scenarios import SCENARIO_VEHICLES
    from cvi.synthetic.identity import vehicle_identity

    if name not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario; choose from {sorted(SCENARIOS)}")
    owner = vehicle_identity(SCENARIO_VEHICLES[name][0])["tenant"]
    if owner != p.tenant_slug:
        raise HTTPException(403, f"scenario '{name}' runs on tenant '{owner}' vehicles; sign in as a '{owner}' user")
    now_ms = int(time.time() * 1000)
    run = (now_ms // 1000) % 100_000
    events = SCENARIOS[name](now_ms, run, random.Random(1234))
    h = {"ingest_ts": str(now_ms)}
    try:
        for e in events:
            ctx.broker.produce(TOPIC_RAW, e["vin"], orjson.dumps(e), h)
        ctx.broker.flush(5)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(503, "event broker unavailable") from ex
    audit(ctx, p, "demo_scenario", "scenario", name, request, {"events": len(events)})
    return {"scenario": name, "events_published": len(events), "vins": sorted({e["vin"] for e in events})}
