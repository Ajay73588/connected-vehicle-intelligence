"""Retry with exponential backoff + jitter, and a circuit breaker."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable

from cvi.common import metrics


class CircuitOpenError(RuntimeError):
    pass


def backoff_delays(retries: int, base: float = 0.1, cap: float = 5.0, jitter: bool = True) -> list[float]:
    delays = []
    for attempt in range(retries):
        d = min(cap, base * (2**attempt))
        delays.append(random.uniform(0, d) if jitter else d)
    return delays


def retry[T](fn: Callable[[], T], retries: int = 3, base: float = 0.1, cap: float = 5.0,
          retry_on: tuple[type[BaseException], ...] = (Exception,), sleep: Callable[[float], None] = time.sleep,
          jitter: bool = True) -> T:
    """Call fn, retrying on `retry_on` with exponential backoff. Re-raises the last error."""
    delays = backoff_delays(retries, base, cap, jitter)
    for attempt in range(retries + 1):
        try:
            return fn()
        except retry_on:
            if attempt == retries:
                raise
            sleep(delays[attempt])
    raise AssertionError("unreachable")


class CircuitBreaker:
    """closed -> (failure_threshold consecutive failures) -> open -> (reset_timeout) -> half-open -> closed/open."""

    def __init__(self, name: str, failure_threshold: int = 5, reset_timeout: float = 10.0,
                 clock: Callable[[], float] = time.monotonic):
        self.name, self.failure_threshold, self.reset_timeout, self._clock = name, failure_threshold, reset_timeout, clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            return "half-open" if self._clock() - self._opened_at >= self.reset_timeout else "open"

    def call[T](self, fn: Callable[[], T]) -> T:
        if self.state == "open":
            raise CircuitOpenError(self.name)
        try:
            result = fn()
        except Exception:
            with self._lock:
                self._failures += 1
                if self._failures >= self.failure_threshold or self._opened_at is not None:
                    self._opened_at = self._clock()
                    metrics.circuit_state.labels(self.name).set(1)
            raise
        with self._lock:
            self._failures = 0
            self._opened_at = None
            metrics.circuit_state.labels(self.name).set(0)
        return result
