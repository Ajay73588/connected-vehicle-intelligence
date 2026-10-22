"""Geohash encode/decode/neighbours, haversine and geofence tests."""

from __future__ import annotations

import math

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DECODE = {c: i for i, c in enumerate(_BASE32)}


def geohash_encode(lat: float, lon: float, precision: int = 7) -> str:
    lat_i, lon_i = (-90.0, 90.0), (-180.0, 180.0)
    bits, bit, ch, even = [16, 8, 4, 2, 1], 0, 0, True
    out: list[str] = []
    while len(out) < precision:
        rng, val = (lon_i, lon) if even else (lat_i, lat)
        mid = (rng[0] + rng[1]) / 2
        if val >= mid:
            ch |= bits[bit]
            rng = (mid, rng[1])
        else:
            rng = (rng[0], mid)
        if even:
            lon_i = rng
        else:
            lat_i = rng
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_BASE32[ch])
            bit, ch = 0, 0
    return "".join(out)


def geohash_decode_bbox(gh: str) -> tuple[float, float, float, float]:
    """Return (lat_min, lat_max, lon_min, lon_max)."""
    lat_i, lon_i, even = [-90.0, 90.0], [-180.0, 180.0], True
    for c in gh:
        cd = _DECODE[c]
        for mask in (16, 8, 4, 2, 1):
            rng = lon_i if even else lat_i
            mid = (rng[0] + rng[1]) / 2
            if cd & mask:
                rng[0] = mid
            else:
                rng[1] = mid
            even = not even
    return lat_i[0], lat_i[1], lon_i[0], lon_i[1]


def geohash_decode(gh: str) -> tuple[float, float]:
    a, b, c, d = geohash_decode_bbox(gh)
    return (a + b) / 2, (c + d) / 2


def geohash_neighbors(gh: str) -> list[str]:
    """The 8 surrounding cells (computed by offsetting the cell centre by one cell size)."""
    lat_min, lat_max, lon_min, lon_max = geohash_decode_bbox(gh)
    lat, lon = (lat_min + lat_max) / 2, (lon_min + lon_max) / 2
    dlat, dlon = lat_max - lat_min, lon_max - lon_min
    out: list[str] = []
    for i in (-1, 0, 1):
        for j in (-1, 0, 1):
            if i == 0 and j == 0:
                continue
            nlat = max(-89.999999, min(89.999999, lat + i * dlat))
            nlon = (lon + j * dlon + 180) % 360 - 180
            out.append(geohash_encode(nlat, nlon, len(gh)))
    return out


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def in_circle(lat: float, lon: float, clat: float, clon: float, radius_m: float) -> bool:
    return haversine_km(lat, lon, clat, clon) * 1000 <= radius_m


def in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray casting point-in-polygon, polygon = [(lat, lon), ...]. O(n)."""
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        yi, xi = polygon[i]
        yj, xj = polygon[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside
