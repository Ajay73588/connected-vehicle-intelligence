"""Telemetry event contract (schema version 1). Also exported as JSON Schema in /schemas for contract tests."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cvi.algorithms.vin import is_valid_vin

SCHEMA_VERSION = 1
MAX_FUTURE_SKEW_MS = 5 * 60 * 1000
_DTC_RE = re.compile(r"^[PCBU][0-3][0-9A-F]{3}$")
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class TelemetryEvent(BaseModel):
    """One vehicle sample. `event_id` makes ingestion idempotent; `seq` is a per-vehicle monotonic counter."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=8, max_length=64)
    tenant: str
    vin: str
    seq: int = Field(ge=0)
    ts: int = Field(description="event time, epoch milliseconds (device clock)", gt=1_500_000_000_000)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed_kmh: float = Field(ge=0, le=300)
    rpm: float | None = Field(default=None, ge=0, le=12000)
    odometer_km: float = Field(ge=0)
    fuel_type: Literal["diesel", "petrol", "ev", "hybrid"] = "diesel"
    fuel_level_pct: float | None = Field(default=None, ge=0, le=100)
    coolant_temp_c: float | None = Field(default=None, ge=-60, le=250)
    oil_temp_c: float | None = Field(default=None, ge=-60, le=250)
    oil_pressure_kpa: float | None = Field(default=None, ge=0, le=1000)
    voltage_v: float | None = Field(default=None, ge=0, le=60)
    vibration_g: float | None = Field(default=None, ge=0, le=50)
    battery_soc_pct: float | None = Field(default=None, ge=0, le=100)
    battery_soh_pct: float | None = Field(default=None, ge=0, le=100)
    battery_temp_c: float | None = Field(default=None, ge=-60, le=150)
    dtc: list[str] = Field(default_factory=list, max_length=16)
    harsh_brake: bool = False
    harsh_accel: bool = False
    idling: bool = False
    speeding: bool = False
    engine_on: bool = True
    trip_id: str | None = None
    obd_raw: str | None = Field(default=None, max_length=128, description="optional raw OBD-II mode-01 frame")

    @field_validator("vin")
    @classmethod
    def _vin(cls, v: str) -> str:
        v = v.upper()
        if not is_valid_vin(v):
            raise ValueError("invalid VIN (length, charset or check digit)")
        return v

    @field_validator("tenant")
    @classmethod
    def _tenant(cls, v: str) -> str:
        if not _TENANT_RE.match(v):
            raise ValueError("invalid tenant slug")
        return v

    @field_validator("dtc")
    @classmethod
    def _dtc(cls, v: list[str]) -> list[str]:
        for code in v:
            if not _DTC_RE.match(code):
                raise ValueError(f"invalid DTC {code!r}")
        return v

    @model_validator(mode="after")
    def _ev_fields(self) -> TelemetryEvent:
        if self.fuel_type != "ev" and self.battery_soc_pct is not None and self.fuel_type == "diesel":
            raise ValueError("diesel vehicle cannot report traction-battery SoC")
        return self


def telemetry_json_schema() -> dict:
    return TelemetryEvent.model_json_schema()
