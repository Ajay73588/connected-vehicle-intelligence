"""OBD-II (SAE J1979) parsing: mode-01 PID responses and mode-03 DTC decoding."""

from __future__ import annotations

_DTC_PREFIX = "PCBU"

# Short catalogue used by the severity logic and the knowledge base (synthetic subset of SAE J2012 codes).
DTC_CATALOG: dict[str, tuple[str, str]] = {
    "P0300": ("Random/multiple cylinder misfire detected", "high"),
    "P0301": ("Cylinder 1 misfire detected", "high"),
    "P0302": ("Cylinder 2 misfire detected", "high"),
    "P0217": ("Engine overtemperature condition", "critical"),
    "P0116": ("Engine coolant temperature sensor range/performance", "medium"),
    "P0171": ("System too lean (bank 1)", "medium"),
    "P0420": ("Catalyst system efficiency below threshold", "medium"),
    "P0562": ("System voltage low", "medium"),
    "P0A80": ("Replace hybrid/EV battery pack", "critical"),
    "P0A0F": ("Engine failed to start", "high"),
    "P0524": ("Engine oil pressure too low", "critical"),
}


def decode_dtc(b1: int, b2: int) -> str:
    """Two raw bytes -> 'P0301'. Bits: [AA][BB][CCCC] [DDDD][EEEE]."""
    letter = _DTC_PREFIX[(b1 >> 6) & 0x3]
    return f"{letter}{(b1 >> 4) & 0x3}{b1 & 0xF:X}{(b2 >> 4) & 0xF:X}{b2 & 0xF:X}"


def encode_dtc(code: str) -> tuple[int, int]:
    if len(code) != 5 or code[0] not in _DTC_PREFIX:
        raise ValueError(f"bad DTC {code!r}")
    first = _DTC_PREFIX.index(code[0]) << 6 | int(code[1], 16) << 4 | int(code[2], 16)
    return first, int(code[3], 16) << 4 | int(code[4], 16)


def parse_mode3_response(hex_frame: str) -> list[str]:
    """'43 03 01 02 17 00 00' -> ['P0301', 'P0217'] (zero pairs are padding)."""
    data = bytes.fromhex(hex_frame.replace(" ", ""))
    if not data or data[0] != 0x43:
        raise ValueError("not a mode-03 response")
    out = []
    for i in range(2, len(data) - 1, 2):
        if data[i] or data[i + 1]:
            out.append(decode_dtc(data[i], data[i + 1]))
    return out


# PID -> (name, bytes, decoder)
_PIDS = {
    0x05: ("coolant_temp_c", 1, lambda d: d[0] - 40),
    0x0C: ("rpm", 2, lambda d: (256 * d[0] + d[1]) / 4),
    0x0D: ("speed_kmh", 1, lambda d: d[0]),
    0x2F: ("fuel_level_pct", 1, lambda d: round(d[0] * 100 / 255, 1)),
    0x5C: ("oil_temp_c", 1, lambda d: d[0] - 40),
}


def parse_mode1_response(hex_frame: str) -> dict[str, float]:
    """Parse '41 0C 1A F8 05 7B' (one or more PID answers) into named signals."""
    data = bytes.fromhex(hex_frame.replace(" ", ""))
    if not data or data[0] != 0x41:
        raise ValueError("not a mode-01 response")
    out: dict[str, float] = {}
    i = 1
    while i < len(data):
        pid = data[i]
        if pid not in _PIDS:
            raise ValueError(f"unsupported PID 0x{pid:02X}")
        name, n, fn = _PIDS[pid]
        chunk = data[i + 1 : i + 1 + n]
        if len(chunk) != n:
            raise ValueError("truncated frame")
        out[name] = fn(chunk)
        i += 1 + n
    return out


def encode_mode1(signals: dict[str, float]) -> str:
    """Inverse of parse_mode1_response (used by the simulator to emit raw OBD frames)."""
    parts = ["41"]
    for pid, (name, _n, _fn) in _PIDS.items():
        if name not in signals:
            continue
        v = signals[name]
        if name == "rpm":
            raw = int(v * 4)
            parts += [f"{pid:02X}", f"{raw >> 8 & 0xFF:02X}", f"{raw & 0xFF:02X}"]
        elif name == "fuel_level_pct":
            parts += [f"{pid:02X}", f"{min(255, int(v * 255 / 100)):02X}"]
        elif name in ("coolant_temp_c", "oil_temp_c"):
            parts += [f"{pid:02X}", f"{max(0, min(255, int(v + 40))):02X}"]
        else:
            parts += [f"{pid:02X}", f"{max(0, min(255, int(v))):02X}"]
    return " ".join(parts)
