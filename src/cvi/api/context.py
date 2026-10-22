"""Application context: lazily-built handles to every backing store, overridable in tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg_pool import ConnectionPool

from cvi.common.config import get_settings
from cvi.messaging.base import Broker
from cvi.storage.mongo_store import MemoryRawStore, RawEventStore, get_raw_store
from cvi.storage.object_store import ObjectStore
from cvi.storage.redis_store import StateStore


@dataclass
class AppContext:
    pool: ConnectionPool          # RLS-enforced API role
    state: StateStore
    raw: RawEventStore | MemoryRawStore
    objects: ObjectStore
    broker: Broker
    worker_pool: ConnectionPool | None = None  # BYPASSRLS role (admin tasks such as erasure)


_ctx: AppContext | None = None


def build_context() -> AppContext:
    from cvi.messaging.factory import get_broker
    from cvi.storage.object_store import get_object_store
    from cvi.storage.pg import get_pool

    s = get_settings()
    return AppContext(pool=get_pool(s.database_url), state=StateStore(), raw=get_raw_store(), objects=get_object_store(),
                      broker=get_broker(), worker_pool=get_pool(s.worker_database_url or s.database_url))


def get_ctx() -> AppContext:
    global _ctx
    if _ctx is None:
        _ctx = build_context()
    return _ctx


def set_ctx(ctx: AppContext | None) -> None:
    global _ctx
    _ctx = ctx


def dep_ctx() -> Any:
    return get_ctx()
