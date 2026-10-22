"""Authentication (JWT, optionally an external OIDC IdP), RBAC permissions and location masking."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request

from cvi.algorithms.geo import geohash_decode, geohash_encode
from cvi.common.config import get_settings

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"vehicles:read", "telemetry:read", "location:precise", "alerts:read", "alerts:write", "analytics:read",
              "agent:use", "agent:act", "admin:audit", "admin:erasure", "demo:run"},
    "fleet_manager": {"vehicles:read", "telemetry:read", "location:precise", "alerts:read", "alerts:write", "analytics:read",
                      "agent:use", "agent:act", "demo:run"},
    "analyst": {"vehicles:read", "telemetry:read", "alerts:read", "analytics:read", "agent:use"},
    "viewer": {"vehicles:read", "alerts:read", "analytics:read"},
}


@dataclass
class Principal:
    user_id: int
    tenant_id: int
    tenant_slug: str
    email: str
    roles: list[str]

    @property
    def permissions(self) -> set[str]:
        return set().union(*(ROLE_PERMISSIONS.get(r, set()) for r in self.roles)) if self.roles else set()

    def can(self, perm: str) -> bool:
        return perm in self.permissions


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=10)).decode()


_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt(rounds=10)).decode()


def verify_password(pw: str, hashed: str | None) -> bool:
    """Always spends one bcrypt comparison, so response time does not reveal whether the account exists."""
    return bcrypt.checkpw(pw.encode(), (hashed or _DUMMY_HASH).encode()) and hashed is not None


def issue_tokens(user: dict) -> dict:
    s = get_settings()
    now = int(time.time())
    base = {"sub": str(user["id"]), "tid": user["tenant_id"], "ts": user["tenant_slug"], "email": user["email"],
            "roles": user["roles"], "iss": s.jwt_issuer, "aud": s.jwt_audience, "iat": now}
    access = jwt.encode({**base, "typ": "access", "exp": now + s.jwt_access_ttl_s, "jti": uuid.uuid4().hex}, s.jwt_secret, "HS256")
    refresh = jwt.encode({**base, "typ": "refresh", "exp": now + s.jwt_refresh_ttl_s, "jti": uuid.uuid4().hex}, s.jwt_secret, "HS256")
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "expires_in": s.jwt_access_ttl_s}


def decode_token(token: str, expected_type: str = "access") -> dict:
    s = get_settings()
    try:
        if s.oidc_jwks_url and jwt.get_unverified_header(token).get("alg") == "RS256":
            key = jwt.PyJWKClient(s.oidc_jwks_url).get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=s.jwt_audience, issuer=s.jwt_issuer)
        else:
            claims = jwt.decode(token, s.jwt_secret, algorithms=["HS256"], audience=s.jwt_audience, issuer=s.jwt_issuer)
    except jwt.PyJWTError as e:
        raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"}) from e
    if claims.get("typ", "access") != expected_type:
        raise HTTPException(401, f"expected a {expected_type} token")
    return claims


def current_principal(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):  # nosec B105 - HTTP auth scheme name, not a credential
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    c = decode_token(auth[7:].strip())
    p = Principal(int(c["sub"]), int(c["tid"]), c["ts"], c.get("email", ""), list(c.get("roles", [])))
    request.state.principal = p
    return p


def require(perm: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.can(perm):
            raise HTTPException(403, f"missing permission {perm}")
        return p
    return dep


def mask_location(p: Principal, lat: float, lon: float) -> tuple[float, float]:
    """Callers without `location:precise` only see the centre of a ~5 km geohash cell (privacy by design)."""
    if p.can("location:precise"):
        return lat, lon
    return geohash_decode(geohash_encode(lat, lon, 5))
