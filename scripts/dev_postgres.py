#!/usr/bin/env python
"""Run a local PostgreSQL 18 (+pgvector) WITHOUT Docker via the `pixeltable-pgserver` wheel (dev/test convenience).
Writes connection settings to .data/dev.env and blocks until Ctrl-C.  Production/compose uses the real postgres image."""

import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pixeltable_pgserver as pg

root = Path(__file__).resolve().parents[1]
(root / ".data").mkdir(exist_ok=True)
srv = pg.get_server(str(root / ".data" / "pg"), cleanup_mode=None)
u = urlparse(srv.get_uri())
host, port = u.hostname, u.port
env = (
    f"ADMIN_DATABASE_URL=postgresql://postgres:@{host}:{port}/postgres\n"
    f"DATABASE_URL=postgresql://cvi_app:cvi_app_dev@{host}:{port}/postgres\n"
    f"WORKER_DATABASE_URL=postgresql://cvi_worker:cvi_worker_dev@{host}:{port}/postgres\n"
)
(root / ".data" / "dev.env").write_text(env)
print(env, flush=True)
print("PostgreSQL ready; Ctrl-C to stop", flush=True)
stop = []
signal.signal(signal.SIGINT, lambda *_: stop.append(1))
signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
while not stop:
    time.sleep(1)
sys.exit(0)
