"""Agent guardrails: input screening, output validation, grounding verification, confirmation tokens."""

from __future__ import annotations

import re
import secrets
from typing import Any

import orjson

MAX_MESSAGE = 1000
MAX_TOOL_OUTPUT_BYTES = 200_000
_INJECTION = [
    r"ignore (all |any |the )?(previous|prior|above) (instructions|rules)",
    r"disregard (the )?(system|previous)", r"reveal (your |the )?(system )?prompt", r"you are now", r"developer mode",
    r"other tenants?", r"all tenants", r"drop table", r"delete from", r";\s*--",
]
_INJ_RE = re.compile("|".join(_INJECTION), re.I)
_SENSITIVE_KEYS = {"password", "password_hash", "token", "secret", "licence_hash", "access_token", "refresh_token"}
_NUM = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?")


class GuardrailViolation(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def screen_input(message: str) -> str:
    msg = "".join(ch for ch in message if ch.isprintable() or ch in "\n\t").strip()
    if not msg:
        raise GuardrailViolation("empty", "Please ask a question about your fleet.")
    if len(msg) > MAX_MESSAGE:
        raise GuardrailViolation("too_long", f"Questions are limited to {MAX_MESSAGE} characters.")
    if _INJ_RE.search(msg):
        raise GuardrailViolation("prompt_injection", "That request tries to override the assistant's rules or reach beyond your fleet, so I can't act on it.")
    return msg


def sanitize_output(obj: Any) -> Any:
    """Strip secret-looking keys recursively and enforce a size cap on what tools may return to the reasoning stage."""
    if isinstance(obj, dict):
        return {k: sanitize_output(v) for k, v in obj.items() if k.lower() not in _SENSITIVE_KEYS}
    if isinstance(obj, list):
        return [sanitize_output(v) for v in obj]
    return obj


def validate_tool_output(out: Any) -> Any:
    clean = sanitize_output(out)
    if len(orjson.dumps(clean, default=str)) > MAX_TOOL_OUTPUT_BYTES:
        raise GuardrailViolation("output_too_large", "tool output exceeded the size limit")
    return clean


def numbers_in(text: str) -> set[float]:
    out = set()
    for m in _NUM.findall(text):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def verify_grounding(text: str, tool_results: list[Any], tolerance: float = 0.051) -> tuple[bool, list[float]]:
    """Every number in `text` must be derivable from the tool results (exact, rounded, percent of a fraction, or a small
    integer such as a list position). Returns (ok, ungrounded_numbers). Used to reject LLM narration that invents figures."""
    blob = orjson.dumps(tool_results, default=str).decode()
    known = numbers_in(blob)
    known |= {v * 100 for v in known if 0 < v <= 1}
    bad = []
    for n in numbers_in(text):
        if abs(n) <= 10 and float(n).is_integer():
            continue  # "top 3", "2 vehicles" style counts
        if any(abs(n - k) <= max(tolerance, abs(k) * 0.01) for k in known):
            continue
        bad.append(n)
    return not bad, bad


def new_confirmation_token() -> str:
    return secrets.token_urlsafe(24)
