"""S3-compatible object storage + partitioned Parquet for warm/cold telemetry.

The application only knows `put/get/list/delete`; S3 (AWS), GCS (interoperability endpoint), Azure Blob (via an
S3 gateway) or MinIO are selected purely by configuration -> cloud portability without code changes.

Layout (Hive-style partitions; DuckDB prunes by tenant/date at query time):
  telemetry/tier=warm/tenant=<slug>/date=YYYY-MM-DD/hour=HH/part-<content-hash>.parquet
  telemetry/tier=cold/tenant=<slug>/date=YYYY-MM-DD/day.parquet            (daily compaction, zstd)
Partitioning rationale: tenant (isolation + per-tenant erasure), date/hour (time-range pruning, retention by
prefix). Vehicle is NOT a path component (100K vehicles would create millions of tiny files); it is a sorted column
inside each file so row-group min/max statistics prune vehicle lookups."""

from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path
from typing import Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from cvi.common.config import get_settings

PARQUET_COLUMNS = [
    "event_id", "tenant", "vin", "seq", "ts", "lat", "lon", "speed_kmh", "rpm", "odometer_km", "fuel_type",
    "fuel_level_pct", "coolant_temp_c", "oil_temp_c", "oil_pressure_kpa", "voltage_v", "vibration_g",
    "battery_soc_pct", "battery_soh_pct", "battery_temp_c", "dtc", "harsh_brake", "harsh_accel", "idling",
    "speeding", "engine_on", "trip_id",
]


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...


class LocalObjectStore:
    def __init__(self, root: str | None = None):
        self.root = Path(root or get_settings().local_object_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("path traversal")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + f".tmp{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, p)  # atomic

    def get(self, key: str) -> bytes:
        return self._p(key).read_bytes()

    def list(self, prefix: str) -> list[str]:
        base = self.root.resolve()
        return sorted(str(p.relative_to(base)).replace("\\", "/") for p in base.rglob("*")
                      if p.is_file() and str(p.relative_to(base)).replace("\\", "/").startswith(prefix) and ".tmp" not in p.name)

    def delete(self, key: str) -> None:
        self._p(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._p(key).exists()


class S3ObjectStore:
    def __init__(self):
        import boto3
        from botocore.config import Config

        s = get_settings()
        self.bucket = s.s3_bucket
        self.c = boto3.client("s3", endpoint_url=s.s3_endpoint, region_name=s.s3_region,
                              aws_access_key_id=s.s3_access_key, aws_secret_access_key=s.s3_secret_key,
                              config=Config(retries={"max_attempts": 5, "mode": "standard"}, s3={"addressing_style": "path"}))

    def ensure_bucket(self) -> None:
        try:
            self.c.head_bucket(Bucket=self.bucket)
        except Exception:
            self.c.create_bucket(Bucket=self.bucket)

    def put(self, key: str, data: bytes) -> None:
        self.c.put_object(Bucket=self.bucket, Key=key, Body=data, ServerSideEncryption="AES256") if not get_settings().s3_endpoint \
            else self.c.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        return self.c.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def list(self, prefix: str) -> list[str]:
        keys, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix, **({"ContinuationToken": token} if token else {})}
            r = self.c.list_objects_v2(**kw)
            keys += [o["Key"] for o in r.get("Contents", [])]
            token = r.get("NextContinuationToken")
            if not token:
                return keys

    def delete(self, key: str) -> None:
        self.c.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self.c.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False


def get_object_store() -> ObjectStore:
    return LocalObjectStore() if get_settings().object_store == "local" else S3ObjectStore()


def _partition(tenant: str, ts_ms: int) -> str:
    t = time.gmtime(ts_ms / 1000)
    return f"telemetry/tier=warm/tenant={tenant}/date={time.strftime('%Y-%m-%d', t)}/hour={t.tm_hour:02d}"


def events_to_table(events: list[dict]) -> pa.Table:
    cols = {c: [e.get(c) for e in events] for c in PARQUET_COLUMNS}
    cols["dtc"] = [",".join(e.get("dtc") or []) for e in events]
    return pa.table(cols)


def write_events(store: ObjectStore, events: list[dict]) -> list[str]:
    """Write events as Parquet, one file per (tenant, date, hour) partition. File name = hash of event ids,
    so rewriting the same batch after a crash is a no-op overwrite (idempotent)."""
    groups: dict[str, list[dict]] = {}
    for e in events:
        groups.setdefault(_partition(e["tenant"], e["ts"]), []).append(e)
    keys = []
    for part, evs in groups.items():
        evs.sort(key=lambda e: (e["vin"], e["ts"]))  # sorted by vehicle -> tight row-group min/max stats
        digest = hashlib.sha256("".join(sorted(e["event_id"] for e in evs)).encode()).hexdigest()[:16]
        key = f"{part}/part-{digest}.parquet"
        buf = io.BytesIO()
        pq.write_table(events_to_table(evs), buf, compression="zstd", row_group_size=50_000)
        store.put(key, buf.getvalue())
        keys.append(key)
    return keys


def read_table(store: ObjectStore, keys: list[str]) -> pa.Table:
    tables = [pq.read_table(io.BytesIO(store.get(k))) for k in keys]
    return pa.concat_tables(tables) if tables else events_to_table([])
