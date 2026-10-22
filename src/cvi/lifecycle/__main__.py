"""Lifecycle service: periodic housekeeping implemented as a sleeping loop inside the service (no OS cron needed, so it
behaves identically in Docker, Kubernetes and on a laptop).

  every LIFECYCLE_INTERVAL_S      refresh KPI materialised view, compact warm->cold Parquet, apply retention
  every SCORING_INTERVAL_S        batch predictive-maintenance scoring of every vehicle
Multiple replicas are safe: each job takes a Redis lock (SET NX) before running."""

from __future__ import annotations

import logging
import os
import signal
import time

from prometheus_client import Counter, start_http_server

from cvi.common.config import get_settings
from cvi.common.logging import setup_logging
from cvi.lifecycle import jobs
from cvi.storage.object_store import get_object_store
from cvi.storage.pg import get_pool
from cvi.storage.redis_store import get_redis

logger = logging.getLogger("lifecycle")
runs = Counter("lifecycle_job_runs_total", "Lifecycle job executions", ["job", "outcome"])


def locked(r, name: str, ttl_s: int) -> bool:
    try:
        return bool(r.set(f"lock:lifecycle:{name}", 1, nx=True, ex=ttl_s))
    except Exception:  # noqa: BLE001 - without Redis we still run (single replica assumption)
        return True


def main() -> None:
    s = get_settings()
    setup_logging("lifecycle", s.log_level)
    start_http_server(int(os.environ.get("LIFECYCLE_METRICS_PORT", "9103")))
    interval = int(os.environ.get("LIFECYCLE_INTERVAL_S", "300"))
    scoring_interval = int(os.environ.get("SCORING_INTERVAL_S", "3600"))
    pool = get_pool(s.worker_database_url or s.database_url)
    store = get_object_store()
    if hasattr(store, "ensure_bucket"):
        store.ensure_bucket()
    r = get_redis()
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    last_scoring = 0.0
    while not stop:
        for name, fn in (("refresh_kpis", lambda: jobs.refresh_kpis(pool)),
                         ("compaction", lambda: jobs.run_compaction(store)),
                         ("retention", lambda: jobs.expire_tiers(store)),
                         ("prune_predictions", lambda: jobs.prune_old_predictions(pool))):
            if not locked(r, name, max(30, interval - 5)):
                continue
            try:
                fn()
                runs.labels(name, "ok").inc()
            except Exception as e:  # noqa: BLE001
                runs.labels(name, "error").inc()
                logger.error("job %s failed: %s", name, e)
        if time.time() - last_scoring >= scoring_interval and locked(r, "scoring", scoring_interval - 5):
            try:
                from cvi.lifecycle.scoring import score_all

                with pool.connection() as c:
                    tenants = [x["id"] for x in c.execute("SELECT id FROM tenants")]
                logger.info("scored %d vehicles", score_all(pool, tenants))
                runs.labels("scoring", "ok").inc()
            except Exception as e:  # noqa: BLE001
                runs.labels("scoring", "error").inc()
                logger.error("scoring failed: %s", e)
            last_scoring = time.time()
        for _ in range(interval):
            if stop:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
