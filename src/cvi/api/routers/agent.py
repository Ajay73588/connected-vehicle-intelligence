from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from cvi.agent import agent as fleet_agent
from cvi.agent.guardrails import GuardrailViolation
from cvi.agent.tools import TOOLS
from cvi.api.common import jsonable
from cvi.api.context import AppContext, dep_ctx
from cvi.api.security import Principal, require
from cvi.storage.pg import tenant_tx, verify_chain

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=40, pattern="^[A-Za-z0-9_-]+$")


class ConfirmIn(BaseModel):
    token: str = Field(min_length=10, max_length=100)
    approve: bool = True


@router.post("/chat")
def chat(body: ChatIn, p: Principal = Depends(require("agent:use")), ctx: AppContext = Depends(dep_ctx)):
    return fleet_agent.ask(ctx, p, body.message, body.session_id)


@router.post("/confirm")
def confirm(body: ConfirmIn, p: Principal = Depends(require("agent:act")), ctx: AppContext = Depends(dep_ctx)):
    try:
        return fleet_agent.confirm(ctx, p, body.token, body.approve)
    except GuardrailViolation as v:
        raise HTTPException(400, v.message) from v


@router.get("/tools")
def tools(p: Principal = Depends(require("agent:use"))):
    return {"tools": [{"name": t.name, "description": t.description, "permission": t.permission, "destructive": t.destructive,
                       "allowed": p.can(t.permission)} for t in TOOLS.values()]}


@router.get("/audit")
def audit_trail(session_id: str | None = Query(None, max_length=40), limit: int = Query(100, ge=1, le=500),
                p: Principal = Depends(require("agent:use")), ctx: AppContext = Depends(dep_ctx)):
    """Users see their own trail; admins see the whole tenant."""
    own_only = not p.can("admin:audit")
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        rows = c.execute("SELECT id, user_id, session_id, step, tool, payload, allowed, ts, hash FROM agent_audit WHERE tenant_id = %s "
                         "AND (%s::text IS NULL OR session_id = %s) AND (%s = false OR user_id = %s) ORDER BY id DESC LIMIT %s",
                         [p.tenant_id, session_id, session_id, own_only, p.user_id, limit]).fetchall()
        ok, n = verify_chain(c, "agent_audit", p.tenant_id) if p.can("admin:audit") else (None, 0)
    return {"items": jsonable(rows), "chain_valid": ok, "chain_length": n}
