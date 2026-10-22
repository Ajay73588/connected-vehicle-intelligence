from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from cvi.api.common import audit
from cvi.api.context import AppContext, dep_ctx
from cvi.api.security import Principal, current_principal, decode_token, issue_tokens, verify_password
from cvi.storage.pg import plain_tx

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("api.auth")
MAX_FAILURES, LOCK_S = 5, 300


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class RefreshIn(BaseModel):
    refresh_token: str


def _lookup(ctx: AppContext, email: str) -> dict | None:
    with plain_tx(ctx.pool) as c:
        return c.execute("SELECT * FROM auth_lookup(%s)", [email.lower().strip()]).fetchone()


@router.post("/login")
def login(body: LoginIn, request: Request, ctx: AppContext = Depends(dep_ctx)):
    email = body.email.lower().strip()
    ip = request.client.host if request.client else "?"
    lock_key = f"loginfail:{email}"
    try:
        fails = int(ctx.state.r.get(lock_key) or 0)
    except Exception:  # noqa: BLE001
        fails = 0
    if fails >= MAX_FAILURES:
        raise HTTPException(429, "too many failed attempts; try again later", headers={"Retry-After": str(LOCK_S)})
    user = _lookup(ctx, email)
    ok = verify_password(body.password, user["password_hash"] if user else None) and bool(user and user["is_active"])
    if not ok:
        try:
            ctx.state.r.incr(lock_key)
            ctx.state.r.expire(lock_key, LOCK_S)
        except Exception:  # noqa: BLE001
            pass
        if user:
            audit(ctx, None, "login_failed", "user", user["id"], request, {"ip": ip}, tenant_id=user["tenant_id"])
        raise HTTPException(401, "invalid credentials")
    try:
        ctx.state.r.delete(lock_key)
    except Exception:  # noqa: BLE001
        pass
    audit(ctx, None, "login", "user", user["id"], request, tenant_id=user["tenant_id"])
    return issue_tokens({"id": user["id"], "tenant_id": user["tenant_id"], "tenant_slug": user["tenant_slug"],
                         "email": user["email"], "roles": user["roles"]})


@router.post("/refresh")
def refresh(body: RefreshIn, ctx: AppContext = Depends(dep_ctx)):
    c = decode_token(body.refresh_token, "refresh")
    try:
        if ctx.state.r.sismember("revoked_jti", c["jti"]):
            raise HTTPException(401, "refresh token already used")
        ctx.state.r.sadd("revoked_jti", c["jti"])  # rotation: each refresh token works once
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        pass
    user = _lookup(ctx, c["email"])
    if not user or not user["is_active"]:
        raise HTTPException(401, "account disabled")
    return issue_tokens({"id": user["id"], "tenant_id": user["tenant_id"], "tenant_slug": user["tenant_slug"],
                         "email": user["email"], "roles": user["roles"]})


@router.get("/me")
def me(p: Principal = Depends(current_principal)):
    return {"user_id": p.user_id, "tenant_id": p.tenant_id, "tenant": p.tenant_slug, "email": p.email, "roles": p.roles,
            "permissions": sorted(p.permissions)}
