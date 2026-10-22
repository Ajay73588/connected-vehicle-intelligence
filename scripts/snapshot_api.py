#!/usr/bin/env python
"""Pin the REST surface (method + path) used by the contract test."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
for k, v in {"JWT_SECRET": "x" * 32, "BROKER": "memory", "REDIS_URL": "fakeredis://", "MONGO_URL": "memory://"}.items():
    os.environ.setdefault(k, v)

from cvi.api.app import create_app  # noqa: E402

spec = create_app().openapi()
lines = sorted(f"{m.upper()} {p}" for p, ops in spec["paths"].items() for m in ops)
out = Path(__file__).resolve().parents[1] / "tests" / "contract" / "openapi_surface.txt"
out.write_text("\n".join(lines) + "\n")
print(f"pinned {len(lines)} operations -> {out}")
