"""Deterministic synthetic fleet identities. Used by BOTH the seed script and the simulator, so VINs match.
All data here is synthetic; no real vehicle or owner data is used anywhere in this project."""

from __future__ import annotations

from dataclasses import dataclass

from cvi.algorithms.vin import make_vin


@dataclass(frozen=True)
class TenantSpec:
    slug: str
    name: str
    plan: str
    home_lat: float
    home_lon: float
    fuel_types: tuple[str, ...]
    wmi: str


TENANTS: tuple[TenantSpec, ...] = (
    TenantSpec("acme-logistics", "Acme Logistics", "enterprise", 41.8781, -87.6298, ("diesel", "petrol"), "1FT"),
    TenantSpec("borealis-ev", "Borealis EV Fleet", "enterprise", 47.6062, -122.3321, ("ev",), "5YJ"),
    TenantSpec("metro-transit", "Metro Transit Authority", "standard", 30.2672, -97.7431, ("hybrid", "diesel"), "1GB"),
)
# Share of the fleet per tenant (out of 20 slots): acme 9, borealis 6, metro 5
_SLOTS = [0] * 9 + [1] * 6 + [2] * 5

MODELS = [
    # (make, model, year, fuel_type, vehicle_class, tank_or_battery_capacity)
    ("Ford", "Transit 250", 2021, "diesel", "van", 95.0),
    ("Ford", "F-150", 2022, "petrol", "truck", 98.0),
    ("Tesla", "Model Y", 2023, "ev", "suv", 75.0),
    ("Rivian", "EDV 500", 2023, "ev", "van", 135.0),
    ("Gillig", "Hybrid Low Floor", 2020, "hybrid", "bus", 150.0),
    ("Blue Bird", "Vision", 2019, "diesel", "bus", 200.0),
]
_MODELS_BY_FUEL = {
    "diesel": [0, 5],
    "petrol": [1],
    "ev": [2, 3],
    "hybrid": [4],
}


def vehicle_identity(i: int) -> dict:
    """Identity for vehicle index i (0-based). Pure function of i."""
    t_idx = _SLOTS[i % 20]
    t = TENANTS[t_idx]
    n_within = i // 20
    fuel = t.fuel_types[n_within % len(t.fuel_types)]
    model_idx = _MODELS_BY_FUEL[fuel][(n_within // 2) % len(_MODELS_BY_FUEL[fuel])]
    # deterministic home spread of ~±0.25° (~28 km) around the tenant's depot
    lat_off = (((i * 2654435761) % 1000) / 1000 - 0.5) * 0.5
    lon_off = (((i * 40503) % 997) / 997 - 0.5) * 0.5
    return {
        "index": i,
        "vin": make_vin(t.wmi, i),
        "tenant": t.slug,
        "tenant_idx": t_idx,
        "fleet_idx": (n_within // 7) % 4,
        "model_idx": model_idx,
        "fuel_type": fuel,
        "home_lat": t.home_lat + lat_off,
        "home_lon": t.home_lon + lon_off,
    }


FLEET_NAMES = ["North Depot", "South Depot", "East Depot", "West Depot"]
