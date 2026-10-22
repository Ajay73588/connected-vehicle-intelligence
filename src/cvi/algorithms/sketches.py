"""Probabilistic data structures: Bloom filter, Count-Min Sketch, Space-Saving Top-K."""

from __future__ import annotations

import hashlib
import heapq
import math


def _hashes(item: str, k: int, m: int) -> list[int]:
    """Kirsch-Mitzenmacher double hashing: k indexes from two 64-bit halves of one blake2b digest."""
    d = hashlib.blake2b(item.encode(), digest_size=16).digest()
    h1 = int.from_bytes(d[:8], "little")
    h2 = int.from_bytes(d[8:], "little") | 1
    return [(h1 + i * h2) % m for i in range(k)]


class BloomFilter:
    """Membership with no false negatives. m = -n ln p / (ln 2)^2 bits, k = (m/n) ln 2 hashes."""

    def __init__(self, capacity: int = 1_000_000, error_rate: float = 0.01):
        self.m = max(8, int(-capacity * math.log(error_rate) / (math.log(2) ** 2)))
        self.k = max(1, round(self.m / capacity * math.log(2)))
        self.bits = bytearray((self.m + 7) // 8)
        self.count = 0

    def add(self, item: str) -> bool:
        """Add item; return True if it was (probably) already present."""
        present = True
        for i in _hashes(item, self.k, self.m):
            byte, bit = divmod(i, 8)
            if not self.bits[byte] & (1 << bit):
                present = False
                self.bits[byte] |= 1 << bit
        if not present:
            self.count += 1
        return present

    def __contains__(self, item: str) -> bool:
        return all(self.bits[i >> 3] & (1 << (i & 7)) for i in _hashes(item, self.k, self.m))

    def reset(self) -> None:
        self.bits = bytearray(len(self.bits))
        self.count = 0


class CountMinSketch:
    """Frequency estimates that never under-count; over-count <= eps*N with prob >= 1-delta."""

    def __init__(self, eps: float = 0.001, delta: float = 0.01):
        self.width = math.ceil(math.e / eps)
        self.depth = math.ceil(math.log(1 / delta))
        self.table = [[0] * self.width for _ in range(self.depth)]
        self.total = 0

    def add(self, item: str, count: int = 1) -> None:
        for row, idx in zip(self.table, _hashes(item, self.depth, self.width), strict=True):
            row[idx] += count
        self.total += count

    def estimate(self, item: str) -> int:
        return min(row[idx] for row, idx in zip(self.table, _hashes(item, self.depth, self.width), strict=True))


class SpaceSavingTopK:
    """Metwally et al. Space-Saving: top-k heavy hitters in O(k) memory, O(log k) per update."""

    def __init__(self, k: int = 10):
        self.k = k
        self.counts: dict[str, int] = {}

    def add(self, item: str, count: int = 1) -> None:
        if item in self.counts or len(self.counts) < self.k:
            self.counts[item] = self.counts.get(item, 0) + count
            return
        victim = min(self.counts, key=self.counts.__getitem__)  # O(k); k is tiny
        floor = self.counts.pop(victim)
        self.counts[item] = floor + count

    def top(self) -> list[tuple[str, int]]:
        return sorted(self.counts.items(), key=lambda kv: -kv[1])


def top_k_exact(scores: dict[str, float], k: int) -> list[tuple[str, float]]:
    """Exact Top-K via a size-k min-heap: O(n log k) time, O(k) space."""
    return heapq.nlargest(k, scores.items(), key=lambda kv: kv[1])
