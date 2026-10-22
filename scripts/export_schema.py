#!/usr/bin/env python
"""Regenerate schemas/telemetry.v1.schema.json from the pydantic event model (the published contract)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvi.schemas.telemetry import telemetry_json_schema  # noqa: E402

out = Path(__file__).resolve().parents[1] / "schemas" / "telemetry.v1.schema.json"
out.write_text(json.dumps(telemetry_json_schema(), indent=2, sort_keys=True) + "\n")
print("wrote", out)
