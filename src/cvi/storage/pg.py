"""PostgreSQL access: connection pools, tenant-scoped transactions (RLS), migrations, hash-chained audit."""

from __future__ import annotations

import hashlib
import logging
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import orjson
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from cvi.common.config import get_settings

logger = logging.getLogger("pg")
def _migrations_dir() -> Path:
    """Source checkout: <repo>/migrations. Container/installed package: $CVI_MIGRATIONS_DIR or ./migrations (WORKDIR /app)."""
    import os

    for cand in (os.environ.get("CVI_MIGRATIONS_DIR"), str(Path(__file__).resolve().parents[3] / "migrations"), str(Path.cwd() / "migrations")):
        if cand and (Path(cand) / "0001_core_schema.sql").exists():
            return Path(cand)
    raise FileNotFoundError("migrations directory not found; set CVI_MIGRATIONS_DIR")


APP_ROLE, WORKER_ROLE = "cvi_app", "cvi_worker"

_pools: dict[str, ConnectionPool] = {}


def get_pool(url: str | None = None, min_size: int | None = None, max_size: int | None = None) -> ConnectionPool:
    s = get_settings()
    url = url or s.database_url
    if url not in _pools or _pools[url].closed:
        pool = ConnectionPool(url, min_size=min_size or s.db_pool_min, max_size=max_size or s.db_pool_max,
                              kwargs={"row_factory": dict_row, "autocommit": False}, open=False, timeout=5)
        pool.open(wait=False)
        _pools[url] = pool
    return _pools[url]


def close_pools() -> None:
    for p in _pools.values():
        p.close()
    _pools.clear()


@contextmanager
def tenant_tx(pool: ConnectionPool, tenant_id: int) -> Iterator[psycopg.Connection]:
    """Transaction with `app.tenant_id` set, so PostgreSQL row-level security applies to every statement."""
    with pool.connection() as conn, conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_id)])
        yield conn


@contextmanager
def plain_tx(pool: ConnectionPool) -> Iterator[psycopg.Connection]:
    with pool.connection() as conn, conn.transaction():
        yield conn


def ping(pool: ConnectionPool) -> bool:
    try:
        with pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- migrations

def migrate(admin_url: str, app_password: str | None = None, worker_password: str | None = None) -> list[str]:
    """Apply pending SQL migrations (ordered by filename) and (re)create the least-privilege roles."""
    s = get_settings()
    app_password, worker_password = app_password or s.app_db_password, worker_password or s.worker_db_password
    applied: list[str] = []
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        conn.execute("SELECT pg_advisory_lock(727274)")  # one migrator at a time
        try:
            for f in sorted(_migrations_dir().glob("*.sql")):
                if f.name in done or (get_settings().skip_vector_index and "hnsw" in f.name):
                    continue
                with conn.transaction():
                    conn.execute(f.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", [f.name])
                applied.append(f.name)
                logger.info("applied migration %s", f.name)
            _ensure_roles(conn, app_password, worker_password)
        finally:
            conn.execute("SELECT pg_advisory_unlock(727274)")
    return applied


def _ensure_roles(conn: psycopg.Connection, app_pw: str, worker_pw: str) -> None:
    for role, pw, extra in ((APP_ROLE, app_pw, "NOBYPASSRLS"), (WORKER_ROLE, worker_pw, "BYPASSRLS")):
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role]).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL("{} ROLE {} LOGIN NOSUPERUSER {} PASSWORD {}").format(
            sql.SQL(verb), sql.Identifier(role), sql.SQL(extra), sql.Literal(pw)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(sql.Identifier(role)))
    conn.execute("REVOKE INSERT, UPDATE, DELETE ON audit_log, agent_audit FROM cvi_app")  # audit is append-only for the API...
    conn.execute("GRANT INSERT ON audit_log, agent_audit TO cvi_app")  # ...insert only
    conn.execute("GRANT EXECUTE ON FUNCTION auth_lookup(TEXT) TO cvi_app")
    conn.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log, agent_audit FROM cvi_worker")
    conn.execute("GRANT SELECT ON mv_fleet_daily_kpis TO cvi_app, cvi_worker")


# ---------------------------------------------------------------- audit hash chain

def _digest(prev: str, body: dict) -> str:
    return hashlib.sha256(prev.encode() + orjson.dumps(body, option=orjson.OPT_SORT_KEYS, default=str)).hexdigest()


def append_audit(conn: psycopg.Connection, table: str, tenant_id: int | None, row: dict) -> int:
    """Append to a hash-chained audit table. Chain is per tenant; an advisory lock serialises writers per tenant."""
    if table not in ("audit_log", "agent_audit"):
        raise ValueError(f"not an audit table: {table}")
    conn.execute("SELECT pg_advisory_xact_lock(%s)", [zlib.crc32(f"{table}:{tenant_id}".encode())])
    prev = conn.execute(
        sql.SQL("SELECT hash FROM {} WHERE tenant_id IS NOT DISTINCT FROM %s ORDER BY id DESC LIMIT 1").format(sql.Identifier(table)),
        [tenant_id],
    ).fetchone()
    prev_hash = prev["hash"] if prev else "GENESIS"
    body = {"tenant_id": tenant_id, **row}
    cols = list(row)
    q = sql.SQL("INSERT INTO {} (tenant_id, {}, prev_hash, hash) VALUES (%s, {}, %s, %s) RETURNING id").format(
        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, cols)), sql.SQL(", ").join(sql.Placeholder() * len(cols)))
    vals = [orjson.dumps(v).decode() if isinstance(v, dict) else v for v in row.values()]
    return conn.execute(q, [tenant_id, *vals, prev_hash, _digest(prev_hash, body)]).fetchone()["id"]


def verify_chain(conn: psycopg.Connection, table: str, tenant_id: int | None) -> tuple[bool, int]:
    """Recompute the chain; returns (ok, rows_checked). Detects edits or deletions of audit rows."""
    rows = conn.execute(
        sql.SQL("SELECT * FROM {} WHERE tenant_id IS NOT DISTINCT FROM %s ORDER BY id").format(sql.Identifier(table)), [tenant_id]
    ).fetchall()
    prev = "GENESIS"
    for r in cast("list[dict[str, Any]]", rows):
        body = {k: v for k, v in r.items() if k not in ("id", "prev_hash", "hash", "ts")}
        if r["prev_hash"] != prev or r["hash"] != _digest(prev, body):
            return False, len(rows)
        prev = r["hash"]
    return True, len(rows)
