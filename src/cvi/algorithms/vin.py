"""VIN validation (ISO 3779 / 49 CFR 565 check digit) and synthetic VIN generation."""

from __future__ import annotations

_TRANSLIT = {c: v for v, cs in {1: "AJ", 2: "BKS", 3: "CLT", 4: "DMU", 5: "ENV", 6: "FW", 7: "GPX", 8: "HY", 9: "RZ"}.items() for c in cs}
_TRANSLIT.update({str(d): d for d in range(10)})
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"  # no I, O, Q


def check_digit(vin: str) -> str:
    total = sum(_TRANSLIT[c] * w for c, w in zip(vin, _WEIGHTS, strict=True))
    r = total % 11
    return "X" if r == 10 else str(r)


def is_valid_vin(vin: str) -> bool:
    if not isinstance(vin, str) or len(vin) != 17:
        return False
    vin = vin.upper()
    if any(c not in _TRANSLIT for c in vin):  # I, O, Q are illegal
        return False
    return vin[8] == check_digit(vin)


def make_vin(wmi: str, serial: int, year_code: str = "R") -> str:
    """Build a valid VIN: WMI(3) + VDS(5) + check + year + plant + serial(6). Deterministic in `serial`."""
    body = list(wmi + "".join(_ALPHABET[(serial * 7 + i * 3) % len(_ALPHABET)] for i in range(5)) + "0" + year_code + "A" + f"{serial % 1_000_000:06d}")
    body[8] = "0"
    body[8] = check_digit("".join(body))
    return "".join(body)
