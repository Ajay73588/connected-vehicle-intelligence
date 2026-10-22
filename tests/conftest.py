"""Shared fixtures.

Unit tests need nothing external. Integration tests use a REAL PostgreSQL (migrations, RLS, SQL) and, when reachable, a
real Redis. PostgreSQL comes from TEST_ADMIN_DATABASE_URL (CI service container) or, locally, from the pixeltable-pgserver
wheel; if neither is available those tests are skipped (never faked)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_TMP = tempfile.mkdtemp(prefix="cvi-test-")
os.environ.update({
    "BROKER": "memory", "REDIS_URL": "fakeredis://", "MONGO_URL": "memory://", "OBJECT_STORE": "local",
    "LOCAL_OBJECT_DIR": os.path.join(_TMP, "objects"), "MODEL_DIR": os.path.join(_TMP, "models"),
    "JWT_SECRET": "test-secret-test-secret-test-secret", "RATE_LIMIT_PER_MINUTE": "100000", "ENV": "test",
})
os.environ.setdefault("SKIP_VECTOR_INDEX", "true")
os.environ.setdefault("VECTOR_BACKEND", "python")

from cvi.common.config import get_settings  # noqa: E402

get_settings.cache_clear()


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)


# --------------------------------------------------------------------------------------------- real PostgreSQL
@pytest.fixture(scope="session")
def pg_urls():
    admin = os.environ.get("TEST_ADMIN_DATABASE_URL")
    server = None
    if not admin:
        try:
            import pixeltable_pgserver as pgs

            d = Path(_TMP) / "pg"
            d.mkdir(parents=True, exist_ok=True)
            server = pgs.get_server(str(d / "data"), cleanup_mode="stop")
            admin = server.get_uri()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no PostgreSQL available for integration tests: {e}")
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS cvi_test WITH (FORCE)")
        c.execute(sql.SQL("CREATE DATABASE cvi_test"))
    from urllib.parse import urlparse, urlunparse

    u = urlparse(admin)
    host = u.hostname
    port = u.port or 5432

    def url(user: str, pw: str) -> str:
        netloc = f"{user}:{pw}@{host}:{port}" if pw else f"{user}@{host}:{port}"
        return urlunparse(("postgresql", netloc, "/cvi_test", "", "", ""))

    admin_test = urlunparse((u.scheme, u.netloc, "/cvi_test", "", "", ""))
    yield {"admin": admin_test, "app": url("cvi_app", "cvi_app_dev"), "worker": url("cvi_worker", "cvi_worker_dev")}
    from cvi.storage.pg import close_pools

    close_pools()
    if server is not None:
        try:
            server.cleanup()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(scope="session")
def seeded_db(pg_urls):
    """Migrated + seeded (600 vehicles, small history) database. Runs the REAL seed script."""
    env = {**os.environ, "ADMIN_DATABASE_URL": pg_urls["admin"], "DATABASE_URL": pg_urls["app"], "WORKER_DATABASE_URL": pg_urls["worker"]}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed.py"), "--vehicles", "600", "--history-alerts", "3000", "--history-days", "5"],
                       env=env, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError("seed failed:\n" + r.stdout[-2000:] + r.stderr[-3000:])
    os.environ.update({"DATABASE_URL": pg_urls["app"], "WORKER_DATABASE_URL": pg_urls["worker"], "ADMIN_DATABASE_URL": pg_urls["admin"]})
    get_settings.cache_clear()
    return pg_urls


@pytest.fixture()
def ctx(seeded_db):
    """Application context wired to the real test PostgreSQL and fresh in-memory broker/state/raw store."""
    import fakeredis

    from cvi.api.context import AppContext, set_ctx
    from cvi.messaging.memory import MemoryBroker
    from cvi.storage.mongo_store import MemoryRawStore
    from cvi.storage.object_store import LocalObjectStore
    from cvi.storage.pg import get_pool
    from cvi.storage.redis_store import StateStore

    broker = MemoryBroker(4)
    broker.ensure_topics()
    c = AppContext(pool=get_pool(seeded_db["app"]), state=StateStore(fakeredis.FakeRedis(decode_responses=True)), raw=MemoryRawStore(),
                   objects=LocalObjectStore(tempfile.mkdtemp(dir=_TMP)), broker=broker, worker_pool=get_pool(seeded_db["worker"]))
    set_ctx(c)
    yield c
    set_ctx(None)


@pytest.fixture()
def client(ctx):
    from fastapi.testclient import TestClient

    from cvi.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client, email: str, password: str = "Demo@12345") -> dict:
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture()
def acme_manager(client):
    return login(client, "fleet_manager@acme.demo")


@pytest.fixture()
def borealis_admin(client):
    return login(client, "admin@borealis.demo")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]

import logging as _logging  # noqa: E402

_logging.raiseExceptions = False  # third-party atexit loggers write to closed streams on shutdown
