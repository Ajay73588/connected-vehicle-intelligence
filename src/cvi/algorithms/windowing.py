"""Event-time sliding window with out-of-order insertion, plus online statistics."""

from __future__ import annotations

import bisect
import math
from collections import deque


class SlidingWindow:
    """Time-bounded window of (ts, value) kept sorted by *event time*.

    - add(): O(log n) search + O(n) worst-case insert for late events (O(1) amortised for in-order data).
    - evict(): amortised O(1) per element (pop from the left).
    The window length is measured against the newest event time seen (the watermark), not wall-clock.
    """

    def __init__(self, length_s: float):
        self.length_s = length_s
        self._ts: deque[float] = deque()
        self._vals: deque[float] = deque()
        self._sum = 0.0

    def __len__(self) -> int:
        return len(self._ts)

    def add(self, ts: float, value: float) -> bool:
        """Insert in event-time order. Returns False if the event is already older than the window."""
        if self._ts and ts < self._ts[-1] - self.length_s:
            return False
        if not self._ts or ts >= self._ts[-1]:
            self._ts.append(ts)
            self._vals.append(value)
        else:
            idx = bisect.bisect_right(self._ts, ts)
            self._ts.insert(idx, ts)
            self._vals.insert(idx, value)
        self._sum += value
        self._evict()
        return True

    def _evict(self) -> None:
        horizon = self._ts[-1] - self.length_s
        while self._ts and self._ts[0] < horizon:
            self._ts.popleft()
            self._sum -= self._vals.popleft()

    @property
    def total(self) -> float:
        return self._sum

    @property
    def mean(self) -> float:
        return self._sum / len(self._ts) if self._ts else 0.0

    def values(self) -> list[float]:
        return list(self._vals)

    def slope_per_s(self) -> float:
        """Least-squares slope of value vs time (trend detection)."""
        n = len(self._ts)
        if n < 3:
            return 0.0
        t0 = self._ts[0]
        xs = [t - t0 for t in self._ts]
        mx, my = sum(xs) / n, self._sum / n
        den = sum((x - mx) ** 2 for x in xs)
        if den == 0:
            return 0.0
        return sum((x - mx) * (y - my) for x, y in zip(xs, self._vals, strict=True)) / den


class OnlineStats:
    """EWMA mean/variance for streaming z-score anomaly detection (O(1) time and space per signal)."""

    def __init__(self, alpha: float = 0.05):
        self.alpha, self.mean, self.var, self.n = alpha, 0.0, 0.0, 0

    def update(self, x: float) -> float:
        """Return the z-score of x against the state *before* updating, then update."""
        if self.n == 0:
            self.mean, z = x, 0.0
        else:
            std = math.sqrt(self.var)
            z = (x - self.mean) / std if std > 1e-9 and self.n >= 20 else 0.0
            d = x - self.mean
            self.mean += self.alpha * d
            self.var = (1 - self.alpha) * (self.var + self.alpha * d * d)
        self.n += 1
        return z
